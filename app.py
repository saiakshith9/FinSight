from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    Response,
)

from flask_sqlalchemy import SQLAlchemy
from snowflake.sqlalchemy import URL

from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

import os
import csv
import json
import time
from io import StringIO
try:
    import boto3
except ImportError:
    boto3 = None
import webbrowser
import google.generativeai as genai
from dotenv import load_dotenv

# =========================================
# ENV + AI CONFIG
# =========================================

load_dotenv(override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is missing from .env")

genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel('gemini-3.5-flash')

# =========================================
# FLASK APP
# =========================================
app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")

if not app.secret_key:
    raise ValueError("SECRET_KEY is missing from .env file")

# =========================================
# DATABASE CONFIG - SNOWFLAKE
# =========================================

SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD")
SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE")

if not all([
    SNOWFLAKE_USER,
    SNOWFLAKE_PASSWORD,
    SNOWFLAKE_ACCOUNT,
    SNOWFLAKE_DATABASE,
    SNOWFLAKE_SCHEMA,
    SNOWFLAKE_WAREHOUSE,
    SNOWFLAKE_ROLE
]):
    print("WARNING: One or more Snowflake environment variables are missing from .env")

app.config['SQLALCHEMY_DATABASE_URI'] = URL(
    account=SNOWFLAKE_ACCOUNT,
    user=SNOWFLAKE_USER,
    password=SNOWFLAKE_PASSWORD,
    database=SNOWFLAKE_DATABASE,
    schema=SNOWFLAKE_SCHEMA,
    warehouse=SNOWFLAKE_WAREHOUSE,
    role=SNOWFLAKE_ROLE
)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# =========================================
# LOGIN MANAGER
# =========================================

login_manager = LoginManager()

login_manager.init_app(app)

login_manager.login_view = 'login'

# =========================================
# TRANSACTION TABLE
# =========================================

class Transaction(db.Model):

    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    date = db.Column(db.String(50))

    category = db.Column(db.String(100))

    amount = db.Column(db.Float)

# =========================================
# BUDGET TABLE
# =========================================

class Budget(db.Model):

    __tablename__ = 'budgets'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    category = db.Column(db.String(100))

    limit_amount = db.Column(db.Float)

# =========================================
# USER TABLE
# =========================================

class User(UserMixin, db.Model):

    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), unique=True, nullable=False)

    email = db.Column(db.String(120), unique=True, nullable=False)

    password = db.Column(db.String(200), nullable=False)

# =========================================
# LOAD USER
# =========================================

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_next_id(model):
    max_id = db.session.query(db.func.max(model.id)).scalar()
    return 1 if max_id is None else int(max_id) + 1


def call_fintrack_lambda(user_id):
    """Call AWS Lambda to generate a Snowflake financial report for the logged-in user."""
    try:
        if boto3 is None:
            return {
                "success": False,
                "error": "boto3 is not installed. Run: pip install boto3"
            }

        lambda_function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        aws_region = os.getenv("AWS_REGION", "us-east-2")
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        aws_session_token = os.getenv("AWS_SESSION_TOKEN")

        if not lambda_function_name:
            return {
                "success": False,
                "error": "AWS_LAMBDA_FUNCTION_NAME is missing from .env"
            }

        if not aws_access_key or not aws_secret_key:
            return {
                "success": False,
                "error": "AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY is missing from .env"
            }

        client_kwargs = {
            "service_name": "lambda",
            "region_name": aws_region,
            "aws_access_key_id": aws_access_key,
            "aws_secret_access_key": aws_secret_key
        }

        if aws_session_token:
            client_kwargs["aws_session_token"] = aws_session_token

        lambda_client = boto3.client(**client_kwargs)

        payload = {
            "user_id": int(user_id)
        }

        response = lambda_client.invoke(
            FunctionName=lambda_function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        if response_payload.get("statusCode") != 200:
            return {
                "success": False,
                "error": response_payload
            }

        body = response_payload.get("body", "{}")

        if isinstance(body, str):
            body = json.loads(body)

        return {
            "success": True,
            "data": body
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

# =========================================
# SAMPLE DATA SEEDER
# =========================================

def seed_sample_data(user_id):
    import random
    from datetime import datetime

    rnd = random.Random(user_id * 9973)

    def r(low, high):
        return round(rnd.uniform(low, high), 2)

    # Get next IDs manually because Snowflake may not auto-generate integer primary keys
    max_tx_id = db.session.query(db.func.coalesce(db.func.max(Transaction.id), 0)).scalar()
    transaction_id = int(max_tx_id) + 1

    max_budget_id = db.session.query(db.func.coalesce(db.func.max(Budget.id), 0)).scalar()
    budget_id = int(max_budget_id) + 1

    current_year = datetime.now().year
    years = [current_year - 2, current_year - 1, current_year]

    sample_transactions = []

    for year in years:
        base_salary = round(rnd.randint(580, 850) * 10, 2)

        # Demo data rule:
        # Previous years get Jan-Dec.
        # Current year gets data only up to the current month.
        # Example in May 2026: 2024 Jan-Dec, 2025 Jan-Dec, 2026 Jan-May.
        if year == current_year:
            month_range = range(1, datetime.now().month + 1)
        else:
            month_range = range(1, 13)

        for month in month_range:
            salary_growth = (year - years[0]) * 180 + (month * 45)
            salary = round(base_salary + salary_growth + r(-150, 250), 2)

            rent = r(1450, 2050) + ((year - years[0]) * 60)
            groceries = r(280, 520) + ((year - years[0]) * 25)
            shopping = r(130, 650)
            dining = r(100, 330)
            transport = r(90, 280)
            utilities = r(130, 280)
            subscriptions = r(40, 120)
            health = r(50, 260)
            entertainment = r(70, 280)
            savings_transfer = r(450, 1100) + ((year - years[0]) * 80)

            sample_transactions.extend([
                {'date': f'{year}-{month:02d}-01', 'category': 'Salary', 'amount': salary},
                {'date': f'{year}-{month:02d}-03', 'category': 'Rent', 'amount': round(rent, 2)},
                {'date': f'{year}-{month:02d}-05', 'category': 'Utilities', 'amount': round(utilities, 2)},
                {'date': f'{year}-{month:02d}-07', 'category': 'Groceries', 'amount': round(groceries, 2)},
                {'date': f'{year}-{month:02d}-10', 'category': 'Transport', 'amount': round(transport, 2)},
                {'date': f'{year}-{month:02d}-12', 'category': 'Dining', 'amount': round(dining, 2)},
                {'date': f'{year}-{month:02d}-15', 'category': 'Subscriptions', 'amount': round(subscriptions, 2)},
                {'date': f'{year}-{month:02d}-18', 'category': 'Shopping', 'amount': round(shopping, 2)},
                {'date': f'{year}-{month:02d}-21', 'category': 'Health', 'amount': round(health, 2)},
                {'date': f'{year}-{month:02d}-24', 'category': 'Entertainment', 'amount': round(entertainment, 2)},
                {'date': f'{year}-{month:02d}-27', 'category': 'Savings Transfer', 'amount': round(savings_transfer, 2)},
            ])

            # Quarterly bonus
            if month in [3, 6, 9, 12]:
                sample_transactions.append({
                    'date': f'{year}-{month:02d}-14',
                    'category': 'Bonus',
                    'amount': r(700, 1800)
                })

            # Occasional travel expenses
            if month in [4, 8, 11]:
                sample_transactions.append({
                    'date': f'{year}-{month:02d}-22',
                    'category': 'Travel',
                    'amount': r(450, 1300)
                })

            # Occasional extra income
            if month in [5, 10]:
                sample_transactions.append({
                    'date': f'{year}-{month:02d}-20',
                    'category': 'Income',
                    'amount': r(300, 900)
                })

    for row in sample_transactions:
        if not is_past_or_today(row['date']):
            continue

        db.session.add(Transaction(
            id=transaction_id,
            user_id=user_id,
            date=row['date'],
            category=row['category'],
            amount=row['amount']
        ))
        transaction_id += 1

    sample_budgets = [
        {'category': 'Rent', 'limit_amount': 2100},
        {'category': 'Groceries', 'limit_amount': 550},
        {'category': 'Dining', 'limit_amount': 350},
        {'category': 'Shopping', 'limit_amount': 600},
        {'category': 'Transport', 'limit_amount': 300},
        {'category': 'Entertainment', 'limit_amount': 280},
        {'category': 'Utilities', 'limit_amount': 280},
        {'category': 'Subscriptions', 'limit_amount': 120},
        {'category': 'Travel', 'limit_amount': 900},
        {'category': 'Health', 'limit_amount': 300},
    ]

    for row in sample_budgets:
        db.session.add(Budget(
            id=budget_id,
            user_id=user_id,
            category=row['category'],
            limit_amount=row['limit_amount']
        ))
        budget_id += 1

    db.session.commit()
# =========================================
# HELPERS
# =========================================

INCOME_CATEGORIES = ['salary', 'income', 'paycheck', 'bonus']


def is_past_or_today(date_text):
    """Return True only when the transaction date is today or earlier."""
    try:
        tx_date = datetime.strptime(str(date_text)[:10], '%Y-%m-%d').date()
        return tx_date <= datetime.now().date()
    except Exception:
        return True


def is_strong_password(password):
    if not password or len(password) < 8:
        return False

    has_upper = any(char.isupper() for char in password)
    has_lower = any(char.islower() for char in password)
    has_digit = any(char.isdigit() for char in password)
    has_special = any(not char.isalnum() for char in password)

    return has_upper and has_lower and has_digit and has_special


PASSWORD_RULE_MESSAGE = (
    "Password must be at least 8 characters and include uppercase, lowercase, number, and special character."
)


def empty_dashboard_data():
    return {
        'transactions': [],
        'transactions_json': [],
        'no_data': True,
        'total_income': 0,
        'total_expenses': 0,
        'savings': 0,
        'savings_rate': 0,
        'average_monthly_income': 0,
        'average_monthly_expenses': 0,
        'average_monthly_savings': 0,
        'lower_spending': 0,
        'potential_savings': 0,
        'category_labels': [],
        'category_values': [],
        'top_spending_category': 'N/A',
        'biggest_transaction': None,
        'monthly_labels': [],
        'monthly_income_data': [],
        'monthly_expense_data': [],
        'all_months': [],
        'all_categories': [],
        'insights': [],
        'recommendations': [],
        'budget_warnings': [],
        'cash_flow_status': 'No Data',
        'cash_flow_message': 'Add transactions to calculate cash flow.',
        'cash_flow_class': 'neutral',
        'predicted_expense': None,
        'predicted_savings': None,
        'prediction_model': 'Manual Linear Regression',
        'prediction_summary': 'Not enough data to generate prediction.',
        'prediction_risk_level': 'N/A',
        'expense_forecast_values': [],
        'expense_forecast_slope': 0,
        'expense_forecast_intercept': 0,
        'financial_health_score': 52,
        'financial_health_status': 'No Data',
        'financial_health_class': 'neutral',
        'financial_health_message': 'Add transactions to calculate your financial health.',
    }


def build_dashboard_data(user_id):
    """Dashboard calculations now come from AWS Lambda."""
    result = call_fintrack_lambda(user_id)

    if not result.get("success"):
        print("LAMBDA DASHBOARD ERROR:", result.get("error", result))
        data = empty_dashboard_data()
        data['lambda_error'] = result.get("error", "Unable to load Lambda dashboard data")
        return data

    data = result.get("data", {}) or {}
    defaults = empty_dashboard_data()
    defaults.update(data)
    return defaults


# =========================================
# AI ASSISTANT CACHE
# =========================================

AI_CONTEXT_CACHE = {}

def get_cached_dashboard_data(user_id, ttl_seconds=60):
    now = time.time()
    cached = AI_CONTEXT_CACHE.get(int(user_id))

    if cached and now - cached["time"] < ttl_seconds:
        return cached["data"]

    data = build_dashboard_data(user_id)
    AI_CONTEXT_CACHE[int(user_id)] = {
        "time": now,
        "data": data
    }
    return data

# =========================================
# HOME
# =========================================

@app.route('/')
def home():
    return redirect(url_for('login'))

# =========================================
# REGISTER
# =========================================

@app.route('/register', methods=['GET', 'POST'])
def register():

    if request.method == 'POST':

        username = request.form.get('username')

        email = request.form.get('email')

        password = request.form.get('password')

        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:

            flash('Passwords do not match.', 'danger')

            return redirect(url_for('register'))

        if not is_strong_password(password):

            flash(PASSWORD_RULE_MESSAGE, 'danger')

            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():

            flash('Email already exists.', 'danger')

            return redirect(url_for('register'))

        new_user = User(
            id=get_next_id(User),
            username=username,
            email=email,
            password=generate_password_hash(password)
        )

        db.session.add(new_user)

        db.session.commit()

        seed_sample_data(new_user.id)

        flash('Account created successfully!', 'success')

        return redirect(url_for('login'))

    return render_template('register.html')
# =========================================
# LOGIN
# =========================================

@app.route('/login', methods=['GET', 'POST'])
def login():

    if request.method == 'POST':

        email = request.form.get('email')

        password = request.form.get('password')

        user = User.query.filter_by(email=email).first()

        if not user:

            flash('User not found.', 'danger')

            return redirect(url_for('login'))

        if not check_password_hash(user.password, password):

            flash('Incorrect password.', 'danger')

            return redirect(url_for('login'))

        login_user(user)

        flash('Login successful!', 'success')

        return redirect(url_for('dashboard'))

    return render_template('login.html')

# =========================================
# DASHBOARD
# =========================================

@app.route('/dashboard')
@login_required
def dashboard():

    data = build_dashboard_data(current_user.id)

    return render_template(
        'dashboard.html',
        username=current_user.username,
        **data
    )
@app.route('/delete_future_transactions')
@login_required
def delete_future_transactions():
    from datetime import date, datetime

    today = date.today()
    all_transactions = Transaction.query.filter_by(user_id=current_user.id).all()

    deleted_count = 0
    future_dates = []

    for transaction in all_transactions:
        try:
            transaction_date = datetime.strptime(transaction.date[:10], "%Y-%m-%d").date()

            if transaction_date > today:
                future_dates.append(transaction.date)
                db.session.delete(transaction)
                deleted_count += 1

        except Exception as e:
            print("DATE PARSE ERROR:", transaction.date, e)

    db.session.commit()

    print("TODAY:", today)
    print("FUTURE TRANSACTIONS DELETED:", deleted_count)
    print("DELETED FUTURE DATES:", future_dates)

    flash(f'{deleted_count} future transactions deleted successfully.', 'success')
    return redirect(url_for('dashboard'))
SIDEBAR_PAGES = {
    'transactions': {
        'title': 'Transactions',
        'subtitle': 'Review recent income, expenses, transfers, and account activity.'
    },
    'budgets': {
        'title': 'Budgets',
        'subtitle': 'Track category spending and keep monthly limits under control.'
    },
    'analytics': {
        'title': 'Analytics',
        'subtitle': 'Compare income, expenses, categories, savings rate, and trends.'
    },
    'predictions': {
        'title': 'Predictions',
        'subtitle': 'See next-month expense and savings forecasts from your history.'
    },
    'ai-assistant': {
        'title': 'AI Assistant',
        'subtitle': 'Ask FinSight about your finances using dashboard-aware context.'
    },
    'reports': {
        'title': 'Reports',
        'subtitle': 'Summarize your current financial picture and key insights.'
    },
}


def render_sidebar_page(page_key):

    page = SIDEBAR_PAGES[page_key]

    data = build_dashboard_data(current_user.id)

    return render_template(
        'sidebar_page.html',
        active_page=page_key,
        page_title=page['title'],
        page_subtitle=page['subtitle'],
        username=current_user.username,
        **data
    )


@app.route('/transactions')
@login_required
def transactions_page():

    return render_sidebar_page('transactions')


@app.route('/budgets')
@login_required
def budgets_page():

    return render_sidebar_page('budgets')


@app.route('/analytics')
@login_required
def analytics_page():

    return render_sidebar_page('analytics')


@app.route('/predictions')
@login_required
def predictions_page():

    return render_sidebar_page('predictions')


@app.route('/ai-assistant')
@login_required
def ai_assistant_page():

    return render_sidebar_page('ai-assistant')


@app.route('/generate_lambda_report')
@login_required
def generate_lambda_report():
    result = call_fintrack_lambda(current_user.id)

    if not result.get("success"):
        print("LAMBDA REPORT ERROR:", result.get("error", result))
        flash("Unable to generate AWS Lambda report right now.", "danger")
        return redirect(url_for('dashboard'))

    lambda_report = result.get("data", {})

    return render_template(
        'report.html',
        username=current_user.username,
        lambda_report=lambda_report,
        health_message=lambda_report.get('health_message') or lambda_report.get('financial_health_message', ''),
        top_category=lambda_report.get('top_spending_category', 'N/A')
    )
# =========================================
# REST API HELPERS
# =========================================

def transaction_to_dict(transaction):
    return {
        "id": transaction.id,
        "date": transaction.date,
        "category": transaction.category,
        "amount": round(float(transaction.amount), 2),
        "is_income": transaction.category.lower() in INCOME_CATEGORIES
    }


def budget_to_dict(budget):
    return {
        "id": budget.id,
        "category": budget.category,
        "limit_amount": round(float(budget.limit_amount), 2)
    }


# =========================================
# REST API ROUTES
# =========================================

@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({
        "success": True,
        "message": "FinSight API is running"
    }), 200


@app.route('/api/dashboard-summary', methods=['GET'])
@login_required
def api_dashboard_summary():
    data = build_dashboard_data(current_user.id)

    return jsonify({
        "success": True,
        "summary": {
            "total_income": data["total_income"],
            "total_expenses": data["total_expenses"],
            "savings": data["savings"],
            "savings_rate": data["savings_rate"],
            "average_monthly_income": data["average_monthly_income"],
            "average_monthly_expenses": data["average_monthly_expenses"],
            "average_monthly_savings": data["average_monthly_savings"],
            "cash_flow_status": data["cash_flow_status"],
            "cash_flow_message": data["cash_flow_message"],
            "cash_flow_class": data["cash_flow_class"],
            "transaction_count": len(data["transactions"])
        }
    }), 200


@app.route('/api/transactions', methods=['GET'])
@login_required
def api_get_transactions():
    transactions = Transaction.query.filter_by(
        user_id=current_user.id
    ).order_by(Transaction.date.desc(), Transaction.id.desc()).all()

    visible_transactions = [t for t in transactions if is_past_or_today(t.date)]

    return jsonify({
        "success": True,
        "transactions": [transaction_to_dict(t) for t in visible_transactions]
    }), 200


@app.route('/api/transactions', methods=['POST'])
@login_required
def api_add_transaction():
    data = request.get_json(silent=True) if request.is_json else request.form

    date = data.get('date')
    category = data.get('category', '').strip()
    amount_raw = data.get('amount')

    if not date or not category or not amount_raw:
        return jsonify({
            "success": False,
            "message": "Please provide date, category, and amount."
        }), 400

    if not is_past_or_today(date):
        return jsonify({
            "success": False,
            "message": "Future-dated transactions are not allowed. Please choose today or an earlier date."
        }), 400

    try:
        amount = float(amount_raw)
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Please enter a valid amount."
        }), 400

    if amount <= 0:
        return jsonify({
            "success": False,
            "message": "Amount must be greater than zero."
        }), 400

    transaction = Transaction(
        id=get_next_id(Transaction),
        user_id=current_user.id,
        date=date,
        category=category,
        amount=amount
    )

    db.session.add(transaction)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Transaction added successfully.",
        "transaction": transaction_to_dict(transaction)
    }), 201


@app.route('/api/transactions/<int:transaction_id>', methods=['DELETE'])
@login_required
def api_delete_transaction(transaction_id):
    transaction = Transaction.query.filter_by(
        id=transaction_id,
        user_id=current_user.id
    ).first()

    if not transaction:
        return jsonify({
            "success": False,
            "message": "Transaction not found."
        }), 404

    db.session.delete(transaction)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Transaction deleted successfully."
    }), 200


@app.route('/api/budgets', methods=['GET'])
@login_required
def api_get_budgets():
    budgets = Budget.query.filter_by(
        user_id=current_user.id
    ).order_by(Budget.category.asc()).all()

    return jsonify({
        "success": True,
        "budgets": [budget_to_dict(b) for b in budgets]
    }), 200


@app.route('/api/budgets', methods=['POST'])
@login_required
def api_save_budget():
    data = request.get_json(silent=True) if request.is_json else request.form

    category = data.get('category', data.get('budget_category', '')).strip()
    limit_raw = data.get('limit_amount', data.get('budget_limit'))

    if not category or not limit_raw:
        return jsonify({
            "success": False,
            "message": "Please provide category and budget limit."
        }), 400

    try:
        limit_amount = float(limit_raw)
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Please enter a valid budget amount."
        }), 400

    if limit_amount <= 0:
        return jsonify({
            "success": False,
            "message": "Budget limit must be greater than zero."
        }), 400

    budget = Budget.query.filter(
        Budget.user_id == current_user.id,
        db.func.lower(Budget.category) == category.lower()
    ).first()

    if budget:
        budget.limit_amount = limit_amount
        message = "Budget updated successfully."
    else:
        budget = Budget(
            id=get_next_id(Budget),
            user_id=current_user.id,
            category=category,
            limit_amount=limit_amount
        )
        db.session.add(budget)
        message = "Budget created successfully."

    db.session.commit()

    return jsonify({
        "success": True,
        "message": message,
        "budget": budget_to_dict(budget)
    }), 200


@app.route('/api/predictions', methods=['GET'])
@login_required
def api_predictions():
    data = build_dashboard_data(current_user.id)

    return jsonify({
        "success": True,
        "predicted_expense": data.get("predicted_expense"),
        "predicted_savings": data.get("predicted_savings")
    }), 200
# =========================================
# DASHBOARD ACTIONS
# =========================================

@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
@login_required
def delete_transaction(transaction_id):

    transaction = Transaction.query.filter_by(
        id=transaction_id,
        user_id=current_user.id
    ).first_or_404()

    db.session.delete(transaction)

    db.session.commit()

    flash('Transaction deleted.', 'success')

    return redirect(url_for('dashboard'))


@app.route('/clear_data', methods=['POST'])
@login_required
def clear_data():

    Transaction.query.filter_by(user_id=current_user.id).delete()

    Budget.query.filter_by(user_id=current_user.id).delete()

    db.session.commit()

    flash('All dashboard data cleared.', 'success')

    return redirect(url_for('dashboard'))


@app.route('/load_demo_data', methods=['POST'])
@login_required
def load_demo_data():

    Transaction.query.filter_by(user_id=current_user.id).delete()

    Budget.query.filter_by(user_id=current_user.id).delete()

    db.session.commit()

    seed_sample_data(current_user.id)

    flash('Realistic demo data loaded successfully.', 'success')

    return redirect(url_for('dashboard'))


@app.route('/export_transactions')
@login_required
def export_transactions():

    transactions = Transaction.query.filter_by(
        user_id=current_user.id
    ).order_by(Transaction.date.desc()).all()

    output = StringIO()

    writer = csv.writer(output)

    writer.writerow(['date', 'category', 'amount'])

    for transaction in transactions:

        if not is_past_or_today(transaction.date):
            continue

        writer.writerow([
            transaction.date,
            transaction.category,
            f'{transaction.amount:.2f}'
        ])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=finance-transactions.csv'
        }
    )

# =========================================
# PROFILE
# =========================================

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():

    if request.method == 'POST':

        current_password = request.form.get('current_password', '')

        new_password = request.form.get('new_password', '')

        confirm_password = request.form.get('confirm_password', '')

        if not check_password_hash(current_user.password, current_password):

            flash('Current password is incorrect.', 'danger')

            return redirect(url_for('profile'))

        if not is_strong_password(new_password):

            flash(PASSWORD_RULE_MESSAGE, 'danger')

            return redirect(url_for('profile'))

        if new_password != confirm_password:

            flash('New passwords do not match.', 'danger')

            return redirect(url_for('profile'))

        current_user.password = generate_password_hash(new_password)

        db.session.commit()

        flash('Password updated successfully.', 'success')

        return redirect(url_for('profile'))

    dashboard_data = build_dashboard_data(current_user.id)

    return render_template(
        'profile.html',
        username=current_user.username,
        email=current_user.email,
        total_tx=len(dashboard_data['transactions']),
        total_income=dashboard_data['total_income'],
        total_expenses=dashboard_data['total_expenses'],
        savings=dashboard_data['savings']
    )
# =========================================
# AI CHATBOT
# =========================================

@app.route('/ai_chat', methods=['POST'])
@app.route('/api/ai-chat', methods=['POST'])
@login_required
def ai_chat():

    request_data = request.get_json(silent=True) or {}

    user_message = request_data.get('message', '').strip()
    scope = request_data.get('scope', 'month')

    if not user_message:
        return jsonify({
            'reply': 'Please type a message.'
        }), 400

    question = user_message.lower().strip()
    question_clean = question.strip(" .,!?:;")

    # FAST replies - no Lambda, no Snowflake, no Gemini
    if question_clean in ["hi", "hello", "hey", "hii", "helo", "good morning", "good afternoon", "good evening"]:
        return jsonify({
            "reply": "Hello! How can I help you?"
        })

    if question_clean in ["help", "what can you do", "what can u do"]:
        return jsonify({
            "reply": "I can help with spending, savings, budgets, and financial insights."
        })

    # Now call Lambda only for finance-related questions
    dashboard_data = get_cached_dashboard_data(current_user.id)

    total_income = dashboard_data.get('total_income', 0)
    total_expenses = dashboard_data.get('total_expenses', 0)
    savings = dashboard_data.get('savings', 0)
    savings_rate = dashboard_data.get('savings_rate', 0)
    biggest_transaction = dashboard_data.get('biggest_transaction')
    category_labels = dashboard_data.get('category_labels', [])
    category_values = dashboard_data.get('category_values', [])
    insights = dashboard_data.get('insights', [])
    recommendations = dashboard_data.get('recommendations', [])
    budget_warnings = dashboard_data.get('budget_warnings', [])

    # Fast direct finance answers - no Gemini
    if "save" in question or "saving" in question or "savings" in question:
        return jsonify({
            "reply": f"Your current savings are ${savings:,.2f}, with a savings rate of {savings_rate}%."
        })

    if "expense" in question or "spending" in question or "spent" in question:
        return jsonify({
            "reply": f"Your total expenses are ${total_expenses:,.2f}."
        })

    if "income" in question or "salary" in question:
        return jsonify({
            "reply": f"Your total income is ${total_income:,.2f}."
        })

    if "top expense" in question or "highest expense" in question or "biggest expense" in question:
        if category_labels and category_values:
            return jsonify({
                "reply": f"Your top expense category is {category_labels[0]} at ${category_values[0]:,.2f}."
            })
        return jsonify({
            "reply": "No expense category data is available yet."
        })

    if "overspending" in question or "over spending" in question or "budget" in question:
        if budget_warnings:
            return jsonify({
                "reply": budget_warnings[0]
            })
        return jsonify({
            "reply": "You are not currently showing major budget warnings."
        })

    if "insight" in question or "recommend" in question or "advice" in question:
        if recommendations:
            return jsonify({
                "reply": recommendations[0]
            })
        if insights:
            return jsonify({
                "reply": insights[0]
            })
        return jsonify({
            "reply": "No major insight is available yet."
        })

    # Gemini only for custom questions
    finance_context = f"""
User finance summary:
Total income: ${total_income:,.2f}
Total expenses: ${total_expenses:,.2f}
Total savings: ${savings:,.2f}
Savings rate: {savings_rate}%
Top categories: {category_labels[:3]}
Category values: {category_values[:3]}
Budget warnings: {budget_warnings[:3]}
Insights: {insights[:3]}
Recommendations: {recommendations[:3]}
Dashboard scope: {scope}
"""

    prompt = f"""
You are FinSight AI, a personal finance assistant.

Rules:
- Keep the answer short.
- Maximum 2 sentences.
- Use only the finance data provided.
- Do not say you cannot access the user's data.
- Do not give legal, tax, or investment guarantees.

{finance_context}

User question:
{user_message}
"""

    try:
        response = model.generate_content(prompt)
        reply = response.text.strip() if response.text else "I could not generate a response right now."

        return jsonify({
            "reply": reply
        })

    except Exception as e:
        print("GEMINI ERROR:", str(e))

        return jsonify({
            "reply": "I can help with your savings, expenses, budgets, and top spending categories."
        }), 200
# =========================================
# LOGOUT
# =========================================

@app.route('/logout')
@login_required
def logout():

    logout_user()

    flash('Logged out successfully!', 'success')

    return redirect(url_for('login'))

# =========================================
# CREATE DATABASE
# =========================================

with app.app_context():
    db.create_all()

# =========================================
# RUN APP
# =========================================

if __name__ == '__main__':

    webbrowser.open('http://127.0.0.1:5000/login')

    app.run(debug=True, use_reloader=False)
    

import json
import os
import math
from datetime import datetime, date
import snowflake.connector

INCOME_CATEGORIES = {"salary", "income", "paycheck", "bonus"}


def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def round2(value):
    return round(float(value or 0), 2)


def month_label(date_text):
    try:
        return datetime.strptime(str(date_text)[:10], "%Y-%m-%d").strftime("%b %Y")
    except Exception:
        return "Unknown"


def month_key(date_text):
    try:
        return datetime.strptime(str(date_text)[:10], "%Y-%m-%d").strftime("%Y-%m")
    except Exception:
        return "0000-00"


def is_past_or_today(date_text, today_text=None):
    """Ignore future-dated transactions using the Flask app's local today value."""
    try:
        tx_date = datetime.strptime(str(date_text)[:10], "%Y-%m-%d").date()
        if today_text:
            today_value = datetime.strptime(str(today_text)[:10], "%Y-%m-%d").date()
        else:
            today_value = date.today()
        return tx_date <= today_value
    except Exception as e:
        print("DATE FILTER ERROR:", date_text, e)
        return False


def linear_regression_forecast(values, months_ahead=1):
    """
    Lightweight Linear Regression implemented manually for AWS Lambda.
    No scikit-learn dependency is needed.

    x = month number
    y = monthly amount
    prediction = slope * next_month + intercept
    """
    clean_values = [float(v or 0) for v in values]

    if not clean_values:
        return {
            "prediction": None,
            "forecast_values": [],
            "slope": 0,
            "intercept": 0,
            "model": "Manual Linear Regression",
            "message": "Not enough data for prediction."
        }

    if len(clean_values) < 2:
        value = round2(clean_values[-1])
        return {
            "prediction": value,
            "forecast_values": [value for _ in range(months_ahead)],
            "slope": 0,
            "intercept": value,
            "model": "Manual Linear Regression",
            "message": "Only one month of data available, so the last value is used as the forecast."
        }

    n = len(clean_values)
    xs = list(range(1, n + 1))
    ys = clean_values

    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)

    slope = numerator / denominator if denominator else 0
    intercept = y_mean - slope * x_mean

    forecast_values = []
    for i in range(1, months_ahead + 1):
        next_x = n + i
        predicted_value = intercept + (slope * next_x)
        forecast_values.append(round2(max(predicted_value, 0)))

    return {
        "prediction": forecast_values[0],
        "forecast_values": forecast_values,
        "slope": round(slope, 4),
        "intercept": round(intercept, 4),
        "model": "Manual Linear Regression",
        "message": "Prediction generated using a lightweight linear regression model inside AWS Lambda."
    }


def lambda_handler(event, context):
    conn = None
    cursor = None

    try:
        user_id = event.get("user_id") if isinstance(event, dict) else None
        today_text = event.get("today") if isinstance(event, dict) else None

        conn = snowflake.connector.connect(
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ["SNOWFLAKE_PASSWORD"],
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=os.environ["SNOWFLAKE_SCHEMA"],
            role=os.environ["SNOWFLAKE_ROLE"]
        )
        cursor = conn.cursor()

        if user_id:
            cursor.execute("""
                SELECT ID, DATE, CATEGORY, AMOUNT
                FROM TRANSACTIONS
                WHERE USER_ID = %s
                ORDER BY DATE DESC, ID DESC
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT ID, DATE, CATEGORY, AMOUNT
                FROM TRANSACTIONS
                ORDER BY DATE DESC, ID DESC
            """)

        transaction_rows = cursor.fetchall()

        if user_id:
            cursor.execute("""
                SELECT CATEGORY, LIMIT_AMOUNT
                FROM BUDGETS
                WHERE USER_ID = %s
            """, (user_id,))
        else:
            cursor.execute("SELECT CATEGORY, LIMIT_AMOUNT FROM BUDGETS")

        budget_rows = cursor.fetchall()

        transactions = []
        total_income = 0.0
        total_expenses = 0.0
        category_totals = {}
        monthly_income = {}
        monthly_expenses = {}
        monthly_order = {}
        all_categories = set()
        biggest_transaction = None

        for row_id, date_value, category_value, amount_value in transaction_rows:
            category = category_value or "Other"
            amount = safe_float(amount_value)
            date_text = str(date_value)[:10] if date_value else ""

            if not is_past_or_today(date_text, today_text):
                continue

            is_income = category.lower() in INCOME_CATEGORIES
            key = month_key(date_text)
            label = month_label(date_text)

            monthly_order[key] = label
            all_categories.add(category)

            tx = {
                "id": int(row_id),
                "date": date_text,
                "category": category,
                "amount": round2(amount),
                "is_income": is_income
            }
            transactions.append(tx)

            if is_income:
                total_income += amount
                monthly_income[key] = monthly_income.get(key, 0.0) + amount
            else:
                total_expenses += amount
                monthly_expenses[key] = monthly_expenses.get(key, 0.0) + amount
                category_totals[category] = category_totals.get(category, 0.0) + amount
                if biggest_transaction is None or amount > biggest_transaction["amount"]:
                    biggest_transaction = {
                        "category": category,
                        "amount": round2(amount),
                        "date": date_text
                    }

        sorted_categories = sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
        category_labels = [name for name, _ in sorted_categories]
        category_values = [round2(value) for _, value in sorted_categories]
        category_breakdown = [
            {
                "category": name,
                "amount": round2(value),
                "percentage": round2((value / total_expenses) * 100) if total_expenses > 0 else 0
            }
            for name, value in sorted_categories
        ]

        sorted_month_keys = sorted(monthly_order.keys())
        monthly_labels = [monthly_order[key] for key in sorted_month_keys]
        monthly_income_data = [round2(monthly_income.get(key, 0.0)) for key in sorted_month_keys]
        monthly_expense_data = [round2(monthly_expenses.get(key, 0.0)) for key in sorted_month_keys]

        savings = total_income - total_expenses
        savings_rate = round((savings / total_income) * 100, 1) if total_income > 0 else 0
        month_count = max(len(monthly_labels), 1)

        if total_income > total_expenses:
            cash_flow_status = "Positive"
            cash_flow_message = "Income is higher than expenses"
            cash_flow_class = "positive"
        elif total_income < total_expenses:
            cash_flow_status = "Negative"
            cash_flow_message = "Expenses are higher than income"
            cash_flow_class = "negative"
        else:
            cash_flow_status = "Balanced" if transactions else "No Data"
            cash_flow_message = "Income and expenses are equal" if transactions else "Add transactions to calculate cash flow."
            cash_flow_class = "neutral"

        lower_spending = 0
        if len(monthly_expense_data) >= 2 and monthly_expense_data[-2] > 0:
            lower_spending = round(((monthly_expense_data[-1] - monthly_expense_data[-2]) / monthly_expense_data[-2]) * 100, 1)

        insights = []
        if total_income > 0:
            if savings_rate >= 20:
                insights.append(f"Great job! Your savings rate is {savings_rate}% — above the recommended 20%.")
            elif savings_rate > 0:
                insights.append(f"Your savings rate is {savings_rate}%. Try to reach 20% for better financial health.")
            else:
                insights.append("Your expenses exceed your income this period. Consider reducing spending.")

        if category_labels:
            insights.append(f"Your top spending category is {category_labels[0]} (${category_values[0]:,.2f}).")

        if lower_spending > 10:
            insights.append(f"Spending rose {lower_spending}% vs last month — check recent transactions.")
        elif lower_spending < -10:
            insights.append(f"Great! You reduced spending by {abs(lower_spending)}% compared to last month.")

        recommendations = []
        if category_labels:
            recommendations.append(f"Consider reducing {category_labels[0]} spend — it's your highest category at ${category_values[0]:,.2f}.")
        if savings_rate < 10 and total_income > 0:
            recommendations.append("Aim to save at least 10% of your monthly income for a financial cushion.")
        if lower_spending > 10:
            recommendations.append(f"Your spending jumped {lower_spending}% this month. Review and cut non-essentials.")

        budget_warnings = []
        for budget_category, limit_amount in budget_rows:
            limit_value = safe_float(limit_amount)
            if limit_value <= 0:
                continue
            spent = category_totals.get(budget_category, 0.0)
            monthly_spent = spent / month_count
            if monthly_spent > limit_value:
                pct = round((monthly_spent / limit_value) * 100)
                budget_warnings.append(
                    f"{budget_category}: normalized monthly spend is ${monthly_spent:,.2f} of ${limit_value:,.2f} monthly limit ({pct}%)."
                )

        expense_forecast = linear_regression_forecast(monthly_expense_data, months_ahead=3)
        predicted_expense = expense_forecast["prediction"]

        savings_history = [round2(inc - exp) for inc, exp in zip(monthly_income_data, monthly_expense_data)]
        savings_forecast = linear_regression_forecast(savings_history, months_ahead=3)
        predicted_savings_value = savings_forecast["prediction"]

        trend = "stable"
        if predicted_savings_value is not None and savings_history:
            if predicted_savings_value > savings_history[-1] + 50:
                trend = "increasing"
            elif predicted_savings_value < savings_history[-1] - 50:
                trend = "decreasing"

        prediction_risk_level = "Low"
        if predicted_expense is not None and monthly_expense_data:
            last_expense = monthly_expense_data[-1]
            if predicted_expense > last_expense * 1.15:
                prediction_risk_level = "High"
            elif predicted_expense > last_expense * 1.05:
                prediction_risk_level = "Medium"

        prediction_summary = (
            f"Next month expense is predicted to be ${predicted_expense:,.2f} using manual Linear Regression."
            if predicted_expense is not None else
            "Not enough data to generate an expense prediction."
        )

        top_category = category_labels[0] if category_labels else "N/A"

        if savings_rate >= 30 and total_income > total_expenses:
            health_status = "Strong"
            health_class = "positive"
            health_message = "Your income is higher than expenses and your savings rate is strong."
        elif savings_rate >= 15 and total_income > total_expenses:
            health_status = "Good"
            health_class = "positive"
            health_message = "Your cash flow is positive and your savings rate is healthy."
        elif savings_rate >= 5:
            health_status = "Moderate"
            health_class = "primary-text"
            health_message = "Your finances are stable, but there is room to improve your savings rate."
        else:
            health_status = "Needs Attention"
            health_class = "negative"
            health_message = "Expenses are high compared to income, so reviewing spending can help."


        financial_health_score = 52
        if total_income > 0:
            financial_health_score = 50
            if savings_rate >= 30:
                financial_health_score += 35
            elif savings_rate >= 20:
                financial_health_score += 28
            elif savings_rate >= 10:
                financial_health_score += 18
            elif savings_rate > 0:
                financial_health_score += 8

            if total_income > total_expenses:
                financial_health_score += 10
            if lower_spending < 0:
                financial_health_score += 5

        financial_health_score = max(0, min(100, int(round(financial_health_score))))

        data = {
            "project": "FinSight",
            "data_as_of": today_text or date.today().isoformat(),
            "no_data": len(transactions) == 0,
            "transactions": transactions,
            "transactions_json": transactions,
            "total_income": round2(total_income),
            "total_expenses": round2(total_expenses),
            "savings": round2(savings),
            "savings_rate": savings_rate,
            "average_monthly_income": round2(total_income / month_count),
            "average_monthly_expenses": round2(total_expenses / month_count),
            "average_monthly_savings": round2(savings / month_count),
            "lower_spending": lower_spending,
            "potential_savings": round2(total_expenses * 0.10),
            "category_labels": category_labels,
            "category_values": category_values,
            "category_breakdown": category_breakdown,
            "top_spending_category": top_category,
            "biggest_transaction": biggest_transaction,
            "monthly_labels": monthly_labels,
            "monthly_income_data": monthly_income_data,
            "monthly_expense_data": monthly_expense_data,
            "all_months": monthly_labels,
            "all_categories": sorted(all_categories),
            "insights": insights,
            "recommendations": recommendations,
            "budget_warnings": budget_warnings,
            "cash_flow_status": cash_flow_status,
            "cash_flow_message": cash_flow_message,
            "cash_flow_class": cash_flow_class,
            "predicted_expense": predicted_expense,
            "predicted_savings": {
                "next_month": predicted_savings_value,
                "forecast_values": savings_forecast["forecast_values"],
                "trend": trend,
                "historical_labels": monthly_labels,
                "historical_values": savings_history,
                "model": savings_forecast["model"],
                "slope": savings_forecast["slope"],
                "intercept": savings_forecast["intercept"]
            } if predicted_savings_value is not None else None,
            "prediction_model": "Manual Linear Regression",
            "prediction_summary": prediction_summary,
            "prediction_risk_level": prediction_risk_level,
            "expense_forecast_values": expense_forecast["forecast_values"],
            "expense_forecast_slope": expense_forecast["slope"],
            "expense_forecast_intercept": expense_forecast["intercept"],
            "financial_health_score": financial_health_score,
            "financial_health_status": health_status,
            "financial_health_class": health_class,
            "financial_health_message": health_message,
            "health_status": health_status,
            "health_class": health_class,
            "health_message": health_message,
            "message": "Dashboard and report data generated successfully from AWS Lambda."
        }

        return {"statusCode": 200, "body": json.dumps(data)}

    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e), "message": "Failed to generate Lambda dashboard data."})}

    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass

# ShopNexa — Commercial deployment & sales kit

## Product positioning
ShopNexa is a lightweight retail management platform for small and growing shops. It combines sales recording, product/stock management, expenses, receipts, reporting, staff roles, activity tracking and data exports.

## Live demo
Public product/marketing page: `/welcome`
Application sign-in: `/login`

## Production security
Before handing the system to a customer:
- Set a permanent `SECRET_KEY`.
- Set `OWNER_PASSWORD`, `MANAGER_PASSWORD`, and `EMPLOYEE_PASSWORD`.
- Use a fresh persistent database for each customer.
- Change all bootstrap credentials after first login.
- Never publish administrator credentials in a public listing.

## Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- Persistent disk mount: `/var/data`
- Database: `/var/data/shop.db`

## Recommended commercial offer
Starter: GHS 2,500–3,500 one-time
Business: GHS 4,000–6,000 with branding/setup/customization
Full source-code ownership: GHS 6,000–10,000 depending on buyer requirements
Optional support: GHS 150–300/month

These are seller positioning prices, not guaranteed valuations.

## What the buyer receives
- Deployable Flask application
- SQLite persistence layer
- Render configuration
- Source code
- User/role management
- Sales, products, expenses and reports
- Receipt pages
- Export tools
- Deployment documentation
- Rebranding/customization capability

## Sales rule
Do not claim features that are not demonstrated in the live product. Show the buyer the actual demo and provide a short test account.

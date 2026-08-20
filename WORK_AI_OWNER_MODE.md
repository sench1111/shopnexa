# ShopNexa Work AI — Owner Power Mode

This build runs Work AI in **Owner Power Mode** while the sales workflow is being repaired.

## What the AI can see

The AI can read the same business information needed for owner-level analysis:
- sales and revenue
- costs, expenses and profit
- product prices and stock
- stock valuation
- reports and trends
- staff/manager activity and sales performance

This does **not** change the logged-in user's actual role and does not grant direct access to owner-only pages.

## Web search

Work AI automatically performs a public web search for **every question**. Users do not need to type "search the web". The app tries Tavily when `TAVILY_API_KEY` is configured and otherwise falls back to DuckDuckGo HTML search.

Set `TAVILY_API_KEY` in Render for a more reliable production search provider.

## Secrets

The AI must never receive or reveal:
- passwords or password hashes
- Flask session cookies
- CSRF tokens
- `SECRET_KEY`
- API keys
- credential-reset tokens

The web-search layer redacts credential-like values before sending a query to a public search provider.

## Restoring normal permissions

Set this Render environment variable to `false`:

`WORK_AI_OWNER_MODE=false`

Then the normal employee/manager/owner AI visibility rules are restored.

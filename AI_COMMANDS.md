# ShopNexa Work AI — Command Mode

Work AI now uses the OpenAI Responses API with function tools. When `OPENAI_API_KEY` is configured, natural-language requests can execute ShopNexa business operations instead of only returning advice.

## Examples

- `Add Rice, category Grocery, selling price 100, cost 70, stock 50, minimum 10.`
- `Change Rice price to 110 and stock to 75.`
- `Show all products.`
- `Record a sale of 2 Cokes.`
- `Record 3 Rice at 110 each.`
- `Add a GHS 50 Transport expense.`
- `Show today's profit.`
- `Show this month's expenses.`
- `Show staff activity.`
- `Change manager's role to owner.`
- `Remove the employee called john.`
- `Change the shop name to ShopNexa Ghana.`
- `Open Products.`
- `Generate an image of a ShopNexa rice promotion.`
- `Create an 8-second video advertisement for ShopNexa.`
- `Search the web for the latest retail trends in Ghana.`

## Security boundary

Work AI never receives passwords, password hashes, session cookies, CSRF tokens, API keys, or credential-reset tokens. Password creation/reset remains in the dedicated secure staff workflow. Business data operations are enabled by `WORK_AI_OWNER_MODE=true`.

## Render environment variables

Required for command AI and media:

- `OPENAI_API_KEY` — set manually in Render Environment; never commit it to GitHub.
- `WORK_AI_MODEL=gpt-5.6`
- `WORK_AI_IMAGE_MODEL=gpt-image-2`
- `WORK_AI_VIDEO_MODEL=sora-2`

`DATABASE_URL` is populated from the existing `shopnexa-db` Render PostgreSQL service by `render.yaml`.

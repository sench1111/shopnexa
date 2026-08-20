# ShopNexa Work AI — Exact Visual Target Update

This build applies the supplied ShopNexa Work AI visual reference as the target for the Work AI screen.

## UI
- Blue/white ShopNexa Work AI identity with dedicated robot logo.
- Desktop three-column AI workspace: assistant capabilities, live chat/report, quick actions.
- Mobile layout collapses into a single-column experience and uses the existing three-line left drawer navigation.
- Web Search Enabled status is visible.
- Today's report card uses live ShopNexa database figures when the page loads.
- Quick prompts for sales, weekly summary, low stock, best sellers, staff activity and web search.
- Security/permission panel remains visible.

## Behaviour
- Existing `/api/work-ai` remains the source for questions.
- CSRF is supplied automatically by the shared responsive fetch wrapper.
- Owner/manager/employee permission rules remain enforced server-side.
- Staff activity remains restricted by role.
- Web search remains automatic for current/external questions.
- No passwords, session secrets or CSRF secrets are passed to the AI.

# InternTrack Roadmap

## Current Differentiator
Gmail auto-sync is the core moat. No competitor (Simplify, Teal, Huntr) automatically pulls application confirmations and status updates from email. Everything is detected without any manual input after applying.

## Why Email > Browser Extension for Recording Applications
Simplify's browser extension captures "Apply clicked" — but users often click Apply without finishing the application. Email confirmation = the company actually received your application. Email is the source of truth, not the click.

## Browser Extension (Right Use Case)
A browser extension is still valuable, but for enrichment not recording:

1. User clicks Apply on any job board
2. Extension saves the job posting URL + title + company + JD in the background
3. User finishes the application → confirmation email arrives
4. Gmail sync creates the entry via LLM classification
5. Extension data gets matched and attached to the entry automatically

Result: every entry has the original job posting link + description attached, with zero manual effort. Solves the "no job link" problem without false positives.

## Features Needed to Compete as a Real Product

### Critical (blockers for public launch)
- Multi-user auth (Google OAuth)
- Per-user Gmail token storage
- Deployment (Docker + hosting)

### High Impact
- Browser extension (enrichment flow above)
- Analytics — response rate %, avg days to hear back, best response companies
- Real notes field — free-form per application, not auto-filled email subject
- Auto-dedup — merge same company+role entries, keep highest status

### Google Sheets Bidirectional Sync
Many students already track applications in Google Sheets. Goal: keep both in sync automatically.

- App change (new entry, status update) → immediately reflected in the linked Sheet
- Sheet change (manual edit, new row, status change) → pulled into the app on next sync
- Conflict resolution: last-modified timestamp wins
- Uses Google Sheets API (same Google OAuth flow as Gmail, just add Sheets scope)
- Sheet columns: Company, Role, Status, Cycle, Applied Date, Notes, Link
- Easy to share with friends, family, career advisors — they can view/edit in Sheets without needing the app

### Nice to Have
- Mobile-responsive UI
- Export to CSV
- Deadline/follow-up reminders
- Job board aggregation

## Competitors
- **Simplify** — browser extension, job board, resume tailoring, manual status updates
- **Teal** — resume builder, job tracker, chrome extension
- **Huntr** — kanban job tracker
- **Jobscan** — resume optimization

## Our Edge Over Simplify
Simplify requires manual status updates. We detect OA invites, interview invites, and rejections automatically from email. Combined with a browser extension for JD capture, this would be a meaningfully better product.

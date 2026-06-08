# PolarMeter GitHub Pages Site

Static policy/support site for PolarMeter TestFlight/App Store preparation.

## Pages

- `index.html` — product/support landing
- `privacy.html` — privacy policy draft
- `terms.html` — terms of use draft
- `support.html` — support page
- `styles.css` — shared styling

## Temporary support email

Current beta contact: `kkack286@gmai.com`

This is a temporary 1st-beta contact and is expected to change before public launch.

## Recommended GitHub Pages settings

- Repository: TBD, recommended `polarmeter`
- Visibility: public if using free GitHub Pages
- Source: GitHub Actions
- Workflow draft: `testflight/github-actions-polarmeter-cache-pages.yml`

Expected URLs after publish, depending on repo name:

- `https://<account>.github.io/polarmeter/`
- `https://<account>.github.io/polarmeter/privacy.html`
- `https://<account>.github.io/polarmeter/terms.html`
- `https://<account>.github.io/polarmeter/support.html`
- `https://<account>.github.io/polarmeter/market-snapshot-latest.json`
- `https://<account>.github.io/polarmeter/market-snapshot-manifest.json`
- `https://<account>.github.io/polarmeter/health.json`

## Cache artifacts

Local preview payload:

- `testflight/github-pages-preview/`

Build locally:

```bash
python3 tools/polarmeter_github_pages_prepare.py
python3 tools/polarmeter_github_pages_smoke.py
```

Required GitHub Actions secrets:

- `TWELVE_DATA_API_KEY`
- `FMP_API_KEY`
- `DATA_GO_KR_SERVICE_KEY`

## Publish note

Do not publish or connect App Store Connect until Hyunjin explicitly approves the account/repo/URL.

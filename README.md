# PolarMeter GitHub Pages Site

Static policy/support site for PolarMeter TestFlight/App Store preparation.

## Pages

- `index.html` — product/support landing
- `privacy.html` — privacy policy draft
- `terms.html` — terms of use draft
- `support.html` — support page
- `styles.css` — shared styling

## Temporary support email

Current beta contact: `kkack286@gmail.com`

## Recommended GitHub Pages settings

- Repository: TBD, recommended `polarmeter-site` or `polarmeter`
- Visibility: public if using free GitHub Pages
- Source: GitHub Actions
- Workflow draft: `testflight/github-actions-polarmeter-cache-pages.yml`

Expected URLs after publish with custom subdomain:

- `https://polarmeter.polarbearworks.com/`
- `https://polarmeter.polarbearworks.com/privacy.html`
- `https://polarmeter.polarbearworks.com/terms.html`
- `https://polarmeter.polarbearworks.com/support.html`
- `https://polarmeter.polarbearworks.com/market-snapshot-latest.json`
- `https://polarmeter.polarbearworks.com/market-snapshot-manifest.json`
- `https://polarmeter.polarbearworks.com/health.json`

Fallback GitHub Pages URL before custom domain:

- `https://<account>.github.io/polarmeter/`

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

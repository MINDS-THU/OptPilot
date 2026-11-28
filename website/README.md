# OptPilot Website

This Docusaurus site is the public project hub for [OptPilot](https://github.com/MINDS-THU/OptPilot) and its local Desktop client. Product claims, commands, release status, and research-package descriptions should stay aligned with the current OptPilot repository.

## Local development

```bash
npm install
npm start
```

The default site path is `http://localhost:3000/OptPilot/`. Most source changes are reflected without restarting the server.

## Validation

```bash
npm run typecheck
npm run build
```

The production build is written to `build/` for both English and Chinese locales.

## Content map

- `src/pages/index.tsx`: homepage structure and product narrative
- `src/pages/index.module.css`: homepage presentation and responsive behavior
- `src/data/homeContent.ts`: shared links to guides, examples and releases
- `src/css/custom.css`: site-wide Docusaurus theme and article styles
- `src/components/OverviewPage/`: product overview embedded in the docs route
- `content/docs/demo/`: Desktop installation and operating guide
- `content/docs/case-studies/`: research-package stories used by the homepage and Research Packages index
- `content/blog/`: release and engineering notes
- `src/content/listingData.ts`: research article titles and summaries for the active locale
- `i18n/zh-CN/`: Chinese UI translations and localized content when available
- `static/img/`: brand, experiment-model, research-package, and social-preview assets

## Source of truth

Before changing product copy, check the current OptPilot source documentation:

- `README.md` for positioning, installation, and release boundaries
- `docs/capabilities.md` for supported execution paths
- `docs/ui.md` and `docs/operations.md` for the current Desktop workflow and execution boundaries
- `docs/devs-gallery.md`, `docs/llm-policy-search.md`, and `docs/or-solving.md` for research-package details
- `pyproject.toml` and `src/optpilot/__init__.py` for the Core release version

The public Pages site combines this project website at the root with MkDocs technical documentation under `/reference/`. Desktop source is maintained in the private OptPilot-Desktop repository. Do not describe Desktop source as part of the public repository.

## Bilingual content

English is the source locale. Homepage and theme strings live in `i18n/zh-CN/code.json`, `navbar.json`, and `footer.json`. Localized docs or notes mirror the English path under the relevant `i18n/zh-CN/` content directory. Missing Chinese articles intentionally fall back to English.

## Deployment

The website is deployed at `https://minds-thu.github.io/OptPilot/`. The OptPilot repository's Pages workflow publishes this site at the root and the MkDocs technical reference under `/reference/`.

To preview locally:

```bash
npm run build
npm run serve
```

Set `SITE_URL` or `SITE_BASE_URL` only when building for a different host or path.

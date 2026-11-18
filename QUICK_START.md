# Dani Agent quick start

This folder contains Dani Agent's Windows source application and launchers. It is not an installer or a standalone executable. It includes no developer accounts, credentials, previous products or private Git history.

## First launch without AI usage

1. Extract the entire ZIP. Do not run the application from inside the ZIP.
2. Install the prerequisites described in [README.md](README.md): Windows 11, Python 3.12 with the Windows `py` launcher, Git for Windows and Node.js. Follow the installation-path requirements in that guide. This bundle does not install them for you.
3. Double-click **Start-Dani-Offline.cmd**. The dashboard opens in your browser. This mode blocks AI sessions and AI-work approvals, so you can inspect the application without consuming model quota.
4. Before building products, read **Start and connect** in [README.md](README.md). Each user must sign in to the official Codex and Claude Code programs with their own eligible accounts. No account or subscription is included.
5. When you agree to the initial setup diagnostics consuming subscription quota, close the offline application and run **Start-Dani-Dashboard.cmd**. Once prerequisites are ready, normal mode can automatically run the initial Claude startup check and Codex diagnostic. Product builds also require approval inside the application.

Do not run Dani Agent as Administrator. Keep its private dashboard launch link to yourself. Settings and the `data` folder created after startup contain private local state.

## Attach a project specification

When creating a new product, you can attach Word `.docx`, text-based PDF, TXT, Markdown, CSV or JSON files. Extracted text becomes project context for the agents; it does not train or fine-tune a model. Scanned PDFs, encrypted PDFs, legacy Word `.doc` files and interpretation of embedded images are unsupported. The PDF library is bundled; no global pip installation is needed.

## Sharing or GitHub

- Share this clean folder or its ZIP. Do not publish the developer's original workspace or its Git history.
- Do not add the generated `data` folder, built products, `.env` files, credentials, logs or private reports to a shared copy. A `.gitignore` is included.
- Dani Agent is free and open source under the Apache License 2.0 (see `LICENSE` and `NOTICE`). Claude Code, Codex and the models they use stay under your provider accounts' own terms and limits.

## Validation and limits

Word/PDF uploads, the 456-test regression and an independent source review passed on the developer's system. A real product built from a Word specification also passed six combined acceptance cases. These results do not guarantee every future build or qualify every Windows installation.

Each new user must connect their own accounts and complete security checks on their own machine. Full requirements and limitations are in [README.md](README.md).

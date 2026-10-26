# Dani Agent quick start

This folder contains Dani Agent's Windows source application and launchers. It is not an installer or a standalone executable. It includes no developer accounts, credentials, previous products or private Git history.

## Start Dani Agent

**Start-Dani-Dashboard.cmd** is the normal launcher. Once prerequisites are ready, normal mode can automatically run one Claude startup check and one Codex sandbox diagnostic. These initial checks consume subscription quota before you approve a product build. There is no separate initial setup-consent toggle; use the optional offline mode below if you only want to inspect the application without AI usage.

1. Extract the entire ZIP, or open your cloned repository folder. Do not run the application from inside the ZIP.
2. Install the prerequisites described in [README.md](README.md): Windows 11, Python 3.12 with the Windows `py` launcher, Git for Windows and Node.js. Follow the installation-path requirements in that guide. This bundle does not install them for you.
3. When you agree to the initial setup checks described above, double-click **Start-Dani-Dashboard.cmd**. The dashboard opens in your browser.
4. Follow **Start and connect** in [README.md](README.md), and let the required checks finish. Each user must sign in to the official Codex and Claude Code programs with their own eligible accounts. No account or subscription is included.
5. Once setup is ready, choose **+ New product**, describe your project, review the session scope and choose **Approve and build**. Product builds require this separate approval inside the application.

Do not run Dani Agent as Administrator. Keep its private dashboard launch link to yourself. Settings and the `data` folder created after startup contain private local state.

## Optional: inspect without AI usage

Double-click **Start-Dani-Offline.cmd** if you only want to explore the dashboard, run model-free checks or prepare product drafts without consuming model quota. This mode disables AI sessions and AI-work approvals, so it cannot build products with AI. It is optional, not a required first step; use **Start-Dani-Dashboard.cmd** for normal operation.

## Attach a project specification

When creating a new product, you can attach Word `.docx`, text-based PDF, TXT, Markdown, CSV or JSON files. Extracted text becomes project context for the agents; it does not train or fine-tune a model. Scanned PDFs, encrypted PDFs, legacy Word `.doc` files and interpretation of embedded images are unsupported. The PDF library is bundled; no global pip installation is needed.

## Sharing or GitHub

- Share this clean folder or its ZIP. Do not publish the developer's original workspace or its Git history.
- Do not add the generated `data` folder, built products, `.env` files, credentials, logs or private reports to a shared copy. A `.gitignore` is included.
- The original `LICENSE` is retained unchanged. An open-source or customer-use license has not been selected. A GitHub-ready file layout does not select a license or grant usage rights.

## Validation and limits

Word/PDF uploads, the 456-test regression and an independent source review passed on the developer's system. A real product built from a Word specification also passed six combined acceptance cases. These results do not guarantee every future build or qualify every Windows installation.

Package checks are recorded in `PACKAGE_INFO.json`. Each new user must connect their own accounts and complete security checks on their own machine. Full requirements and limitations are in [README.md](README.md).

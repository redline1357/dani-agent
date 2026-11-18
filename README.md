# Dani Agent

Dani Agent builds small websites and Python applications on a single Windows PC. Claude plans the work, Codex implements each step, local tests run, and a separate Claude session reviews the result. Accepted steps are saved in a private product folder.

Free and open source (Apache-2.0). It runs on your own PC with your own Claude and Codex sign-ins: no Dani Agent account, no server in the middle, no API keys. Provider fees and usage limits apply.

**Early preview.** Dani Agent is tested on the developer's Windows 11 PC: the offline suite passes, an independent source review passed, and a real product built from a Word specification passed independent acceptance checks. Other Windows installations have not been qualified yet, and no local result guarantees that every future build will succeed.

Website: [buildhub.dk/dani-agent](https://buildhub.dk/dani-agent/). Download the latest Windows zip from [Releases](https://github.com/redline1357/dani-agent/releases/latest). Quick start: [QUICK_START.md](QUICK_START.md). For a first look without AI sessions, double-click **Start-Dani-Offline.cmd**. This is a Python source application, not a standalone executable or an installer.

## Requirements

- **Windows 11**, using a normal, non-administrator account. This is the Windows version tested for this candidate; Windows 10 and other operating systems are not qualified.
- **Python 3.12 and the Windows `py` launcher**, from [python.org](https://www.python.org/downloads/windows/). The supplied `.cmd` launchers use `py -3.12`, so a newer Python alone does not satisfy those launchers. The Connect page accepts a Python 3.12+ runtime. Install the runtime for all users, outside the Windows user profile, because the sandbox cannot read a per-user installation. The main application uses Python built-ins. PDF text extraction uses the project-local pypdf 6.21.2 copy in `vendor/`; no global pip installation is required. Its [BSD license](vendor/pypdf.LICENSE) is included. Dani Agent never downloads parser packages automatically.
- **Git for Windows**, installed for all users outside the Windows user profile, from [git-scm.com](https://git-scm.com/download/win).
- **Node.js**, installed at `C:\Program Files\nodejs\node.exe`. The current self-test requires this runtime even when you intend to build only Python products. Web products use Node built-ins; no npm packages are installed.
- **Official Codex and Claude Code Windows programs**, with each user signed in through the provider's own flow and entitled to use the required models. Dani Agent confirms the installed programs before running work.
- A private work directory on a fixed local NTFS drive, outside the Windows user profile. The default is `C:\DaniAgentWork`.

The current Dani Agent workflow accepts subscription sign-ins and rejects API-key/Console sign-ins and provider-routing overrides. It has no API-billing fallback. These are this candidate's implementation limits, not a statement that the providers require subscription authentication or forbid other methods.

## Start and connect

Keep this folder in a location you control. Do not run Dani Agent as administrator.

For initial inspection without AI sessions, double-click **Start-Dani-Offline.cmd**, or open a terminal in this folder and run:

```text
py -3.12 -I -B DaniDashboard.py --offline-only
```

This mode permits model-free checks and product drafts while disabling AI sessions and approvals. Before using the normal **Start-Dani-Dashboard.cmd** launcher, explicitly decide whether to authorize its automatic setup sessions: one Claude zero-tool startup check and one Codex sandbox diagnostic when their prerequisites are met. These checks use subscription sessions. The current dashboard starts them automatically in live mode; it does not offer a separate initial setup-consent toggle. Remain in model-free mode if that scope has not been approved.

The dashboard opens on `http://127.0.0.1:8765` through a private launcher link. Keep that link private.

1. On **Connect accounts**, choose **Find programs**, then confirm the intended Codex, Claude Code, Python and Git installations. Dani Agent records paths, versions and hashes. Recognized provider updates go through confirmation checks; an unrecognized change stops work for attention.
2. Complete sign-in through each official program's own browser flow, then choose **Check again**. Do not enter credentials into product descriptions or support reports. Provider programs manage their own cached credentials; Codex can store these in a local file or an operating-system credential store. See [OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth).
3. Create the private work folder or choose another eligible folder. Dani Agent refuses to change the permissions of an existing nonprivate directory. Folder relocation has recovery checks; wait for any recovery to finish before building.
4. Let the model-free self-test finish. Its duration depends on the machine. In authorized live mode, the startup and sandbox checks follow. A failed automatic setup check waits for a retry instead of being retried on every restart.
5. Review the models the installation makes available. Product steps choose model and effort automatically; the available choices depend on the confirmed programs and account access.

Managed settings or provider environment overrides can block readiness. Review those with the owner or administrator; do not remove organizational controls merely to pass setup.

## Build and monitor

Choose **+ New product**, select the product type, and describe the intended result. Read the session scope before choosing **Approve and build**.

### Attach a project specification

The **Reference files** field in **+ New product** accepts text-based PDF, Word `.docx`, UTF-8 text, Markdown, CSV and JSON. The pinned project-local PDF parser was installed with owner approval and verified through the same sandboxed reader used by the dashboard. Legacy `.doc`, encrypted PDFs and scanned/image-only PDFs are not supported. Images and document layout are not interpreted. Word extraction covers body paragraphs and tables; headers, footers, footnotes and embedded objects are not included, so check the extracted preview.

Select up to five files, each at most 5 MiB. Extraction is limited to 25,000 characters per document and 50,000 characters for the complete combined context, including labels. Oversized text is refused, not silently truncated. Preview the extracted text before creating the project; the typed description may be empty when an attachment supplies the specification.

Documents are parsed locally in a network-disabled MXC sandbox with a 30-second limit and a memory-limited Windows Job. No model session is used to upload or extract text. Temporary parser copies are removed; the original and extracted text remain in the private product metadata store, outside generated code and downloadable product archives. Unbound files can be removed from the form. **Manage unused uploads** recovers files from closed or abandoned drafts and permits explicit deletion; it never lists or deletes a created product's bound documents. Once attached to a created product, their hashes and ownership are fixed so the approved context cannot silently change. This version has no product-document retention/deletion screen.

On build approval, the extracted text is sent as project context to the planner, developer and reviewer. **Attaching a file does not train or fine-tune a model.** Do not upload passwords, API keys or other information you are not authorized to share with the selected AI providers. Draft fields and attachment references survive a reload in the same browser tab; extracted document text is not stored in browser session storage.

If the Dani Agent source needs a new independent review, that approval covers up to **three sequential Claude source-review batches**. The exact batch count is bound to the prepared review. There are no retries within that review task. Build and initialized source-review sessions have no fixed active wall-clock deadline; inactivity checks, cancellation, and separate setup/test limits still apply.

Product retries are a separate policy. Failed steps can be attempted again in fresh workspaces, with increasing waits up to one hour. Automatic resume and polish can also create further sessions under the approved workflow. **The displayed per-step allowances are not a total build-session cap.** Repeated attempts consume subscription allowance and may never succeed. Dani Agent cannot see the remaining quota, guarantee provider availability, or promise completion.

Use **Cancel** to stop a product and its automatic continuation. Interrupted builds retain accepted steps; eligible work may resume in a fresh attempt after a restart or wait. A cancelled request must not resume automatically. Source and provider changes require current validation and source-bound authorization; an invalidated approval is not reused.

When a product is ready, use **Preview**, **Open folder**, or **Download zip**, and review the output against your requirements. Product tests and AI review do not replace independent acceptance checks.

## Data and protection boundaries

Products live under the configured work directory's `products` folder. Dani Agent settings and history live in this application's `data` folder. Keep both private and back them up before migration or release work.

Live AI work sends task text, source, diffs, test output and review evidence to the selected providers through their official programs. A local dashboard does not make those AI sessions offline. Generated product execution and tests use the configured sandbox; launched work is supervised with Windows Job containment. The Claude reviewer is configured with no tools, MCP servers or hooks. These controls must pass on the actual host and exact application source.

The intended product scope is single-user local software using Python's standard library or Node/browser built-ins. Accounts, hosted services, downloaded dependencies and arbitrary future project types are outside this candidate's validation scope.

## License

Dani Agent is free and open source under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. The bundled pypdf dependency keeps its own [BSD 3-Clause license](vendor/pypdf.LICENSE).

The license covers Dani Agent only. Claude Code and Codex are separate programs with their own terms, and your use of them, and of the models they call, stays subject to your provider accounts and their usage limits. Dani Agent is not affiliated with or endorsed by Anthropic or OpenAI.

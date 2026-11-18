# Dani Agent: how to start

Dani Agent is a folder, not an installer. You start it by double-clicking one file.

## The two files to know

| Double-click | What it does |
|---|---|
| **Start Dani Agent.cmd** | Opens Dani Agent in your browser. Use this one. |
| **Start Dani Agent (no AI).cmd** | The same, with AI switched off. Free: look around before you connect anything. |

Everything else in the folder is Dani Agent itself. You never need to open it.

## Before the first start (once per PC)

Install these with their normal installers. Use a normal Windows account, not "Run as administrator".

1. **Python 3.12** from [python.org](https://www.python.org/downloads/windows/). In the installer choose **Customize installation**, tick **py launcher** and, on the next page, **Install Python 3.12 for all users**.
2. **Git for Windows** from [git-scm.com](https://git-scm.com/download/win). The default choices are fine.
3. **Node.js** (the LTS version) from [nodejs.org](https://nodejs.org/). The default choices are fine.
4. **Claude Code**, with the Windows installer from [Claude Code setup](https://docs.claude.com/en/docs/claude-code/setup). Sign in with your Claude plan.
5. **Codex**, the Codex app from the Microsoft Store or [openai.com/codex](https://openai.com/codex). Sign in with your ChatGPT plan.

Dani Agent uses your own plans: their prices and usage limits apply. It never asks for passwords or API keys.

## First start

1. **Extract the whole zip**: right-click `DaniAgent-windows.zip` → **Extract All**. Do not start it from inside the zip.
2. Open the extracted `DaniAgent` folder and double-click **Start Dani Agent.cmd**. Your browser opens Dani Agent.
   If Windows shows "Windows protected your PC", choose **More info** → **Run anyway**. This appears because the file came from the internet.
3. The **Connect accounts** page appears. Follow it from top to bottom:
   1. **Find programs**, then choose **Use this** for each program it found.
   2. Sign in to Claude and Codex if it asks, then choose **Check again**.
   3. **Create private folder**. Your products are kept there (`C:\DaniAgentWork`).
4. Wait for the **automatic safety checks**. The self-test uses no AI and can take 10 to 40 minutes. Then a tiny Claude check and a tiny Codex check run once.
5. When the page says **Ready to build**, you are set.

## Build something

1. Choose **+ New product**, pick **Website** or **Python app**, give it a name and describe what it should do. You can also attach a Word, PDF or text specification.
2. Choose **Create draft**. Nothing runs yet.
3. Read **What you approve**, then choose **Approve and build**.
4. Watch the steps. Dani Agent keeps going by itself, also after a pause or a restart. **Cancel** stops it.
5. When it says **Ready**: **Preview** shows a website right in the page, **Open folder** shows the files, **Download zip** saves a copy.

## Next times

Double-click **Start Dani Agent.cmd**. Your products and settings are still there. To stop Dani Agent, restart the PC or end `pythonw.exe` in Task Manager.

## If something goes wrong

- **"Dani Agent needs Python 3.12"**: install Python as in step 1 above, with both ticks, then try again.
- **The Connect page shows a red warning about environment variables**: close Dani Agent and start it again by double-clicking the file in File Explorer, not from a terminal or a developer tool.
- **A product says "Paused"**: it goes on by itself, and the page says when. You don't need to do anything.
- Questions or problems: [github.com/redline1357/dani-agent/issues](https://github.com/redline1357/dani-agent/issues). Remove private details first.

## Good to know

- Your products, settings and history stay on your PC. While it builds, your description and code are sent to Anthropic and OpenAI through their own programs, under your accounts.
- Keep the browser link private while Dani Agent runs.
- Dani Agent is an early preview, free and open source under the Apache License 2.0. Review what it builds before you rely on it.

More detail: [README.md](README.md). Website: [buildhub.dk/dani-agent](https://buildhub.dk/dani-agent/).

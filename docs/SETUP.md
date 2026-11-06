# Setup

## Install

From the repo root:

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
python -m streamlit run app.py --server.address 127.0.0.1
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Set-Content .env "ANTHROPIC_API_KEY=sk-ant-..."
python -m streamlit run app.py --server.address 127.0.0.1
```

`--server.address 127.0.0.1` keeps the page on your own machine. By default
Streamlit listens on every network interface.

## The "No module named 'anthropic'" error

If the page loads but asking a question fails with
`No module named 'anthropic'`, nothing is wrong with the code. Streamlit is
being run by a **different Python interpreter** than the one that has the
project's dependencies installed.

This happens because `streamlit` is a console script. When you type
`streamlit run ...`, your shell finds whichever `streamlit` executable comes
first on `PATH`, and that executable is hardwired to the interpreter it was
installed under. If `streamlit` was installed globally (or with Homebrew,
or with `pipx`) and `anthropic` was installed into a virtualenv, the two
never see each other. The page itself starts fine, because it only needs
`streamlit`. The failure surfaces later, the first time the pipeline
imports `anthropic`.

Two details in the commands above prevent it:

- `python -m pip install` instead of `pip install` installs into the
  interpreter you just activated, not whichever `pip` is first on `PATH`.
- `python -m streamlit run` instead of `streamlit run` launches Streamlit
  *as a module of that same interpreter*, so the app can import everything
  you just installed.

## Missing API key

If `.env` is missing or has no `ANTHROPIC_API_KEY`, the page shows a
readable message saying so instead of a stack trace. Create `.env` in the
repo root as shown above. It is gitignored, so it can't be committed by
accident.

# Setup

## The "No module named 'anthropic'" error

If the app loads fine but the **Ask** page fails with
`Could not import cli/ask.py: No module named 'anthropic'`, nothing is wrong
with the code. Streamlit is being run by a **different Python interpreter**
than the one that has the project's dependencies installed.

This happens because `streamlit` is a console script. When you type
`streamlit run ...`, your shell finds whichever `streamlit` executable comes
first on `PATH`, and that executable is hardwired to the interpreter it was
installed under. If `streamlit` was installed globally (or with Homebrew,
or with `pipx`) and `anthropic` was installed into a virtualenv, the two
never see each other. The app itself starts fine, because it only needs
`streamlit` — the failure surfaces later, at the exact moment `cli/ask.py`
tries to `import anthropic`.

Confirm the diagnosis by adding this temporarily near the top of
`view/app.py`:

```python
import sys; st.write(sys.executable)
```

If that path is not inside your virtualenv, that's the problem.

## Fix: one interpreter, invoked explicitly

From the repo root:

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run view/app.py
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run view/app.py
```

Two details are doing the work here:

- `python -m pip install` instead of `pip install` — installs into the
  interpreter you just activated, not whichever `pip` is first on `PATH`.
- `python -m streamlit run` instead of `streamlit run` — launches Streamlit
  *as a module of that same interpreter*, so the app is guaranteed to be able
  to import everything you just installed.

Use `python -m streamlit run view/app.py` as the documented command for this
project. It costs seven extra characters and removes an entire class of
confusing environment bug.

## Full pipeline, cold start

```bash
# 0. one-time: API key
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# 1. pull raw trial records from CT.gov
python ingest/pull_trials.py --max-pages 2

# 2. parse into data/trials.db and compute materiality hashes
python normalize/build_db.py

# 3. generate cached summaries -- costs money, so cap it first
python enrich/run_enrichment.py --dry-run
python enrich/run_enrichment.py --limit 10

# 4. run the dashboard
python -m streamlit run view/app.py
```

Steps 1, 2, and 4 are free and repeatable. Only step 3 spends tokens, and
only for trials that are new or whose materiality hash has changed — a
second run over an unchanged dataset reports `0 trial(s) need enrichment`
and costs nothing.

## Optional: pin a theme for consistent screenshots

`view/app.py` styles its cards against Streamlit's theme variables, so it
follows whatever theme is active. If you want screenshots to look identical
on every machine, create `.streamlit/config.toml`:

```toml
[theme]
base = "light"
primaryColor = "#2563eb"
backgroundColor = "#ffffff"
secondaryBackgroundColor = "#f6f8fa"
textColor = "#111418"
font = "sans serif"
```

Swap `base = "dark"` with `backgroundColor = "#0e1117"` and
`textColor = "#e6e9ef"` for the dark variant. The cards adapt to either
without any CSS changes.

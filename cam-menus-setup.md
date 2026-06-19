# Project Setup: `cam-menus`

A step-by-step guide to creating a new Git repository and isolated Python
environment for the Cambridge college menu extraction project. Windows /
PowerShell friendly.

**Core principle:** build and inspect the frame before adding anything that does
work. Git, environment isolation, and ignored secrets come first — before any
pipeline logic moves into the repo.

---

## Target Structure

```
cam-menus/
├── .git/                  # Git's internal records (from git init)
├── .gitignore             # what Git must never track
├── .env                   # your GEMINI_API_KEY — NEVER committed
├── .env.example           # safe template showing what .env needs
├── README.md              # project title block & run instructions
├── requirements.txt       # Python dependencies (your materials list)
├── notebooks/             # exploration — the POC notebook lives here
│   └── cambridge_menus_poc.ipynb
└── src/                   # production code (added later, when you port)
    └── __init__.py
```

Separation of concerns: exploratory work lives in `notebooks/`, production code
in `src/`, and the root holds only project-level concerns. Notebooks are messy by
nature; source is code that runs unattended. Keeping them apart is like keeping
your lab notebook out of the final report.

---

## Step 1 — Make the folder and initialize Git

```powershell
mkdir cam-menus
cd cam-menus
pwd                  # confirm you're in ...\cam-menus
git init             # creates the hidden .git\ — version control now active
git status           # shows "No commits yet"
```

Your global Git identity (name, email, default branch) is machine-wide, so it
carries over from previous projects. No need to reconfigure.

---

## Step 2 — Isolate the Python environment

```powershell
python -m venv .venv          # creates the sandbox in a .venv folder
.venv\Scripts\activate        # activate it (PowerShell)
```

Your prompt should now show `(.venv)`.

If PowerShell blocks the activation script with an execution-policy error (a
common Windows default), run this once per user, then re-run the activate line:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

To leave the environment later: `deactivate`.

---

## Step 3 — Create `.gitignore` first, before any commit

Deliberate ordering: Git should ignore secrets from the very first snapshot, so
nothing sensitive ever enters history. Removing a leaked key later is painful,
and it stays exposed the whole time it is in history.

```powershell
New-Item .gitignore
```

Paste this into the file:

```gitignore
# Secrets — NEVER enter version control
.env

# Python
__pycache__/
*.pyc
.venv/

# Jupyter
.ipynb_checkpoints/      # auto-saved notebook checkpoints, pure noise

# Pipeline output — regenerated data, not source
unified_menus.json       # the cache your pipeline writes; don't track it

# OS / editor cruft
.DS_Store
*.log
```

**Why the two project-specific entries:**

- `.ipynb_checkpoints/` is Jupyter's autosave litter — never useful in history.
- `unified_menus.json` is *generated output*, not source. Commit the recipe, not
  the cake. Anyone with the code and a key can regenerate it, so tracking it just
  creates noise and merge conflicts.

> **Note for later:** notebooks store their cell *outputs* inside the file, which
> can bloat history and occasionally leak data shown in a cell. Committing the
> notebook as-is is fine for now. When you start collaborating, look into
> stripping output on commit.

---

## Step 4 — The safe secrets template

Your real `.env` is ignored, so anyone cloning the repo cannot know what it
should contain. `.env.example` is committed and shows the shape without the value.

```powershell
New-Item .env, .env.example
```

**`.env`** (real key, ignored by Git):

```
GEMINI_API_KEY=your-actual-key-here
```

**`.env.example`** (safe template, committed):

```
GEMINI_API_KEY=
```

Config-in-environment principle: the code reads the key from the environment,
never from a hardcoded value, so the same code runs on your laptop and on a
server with only the environment differing.

---

## Step 5 — Drop in the notebook and dependencies

```powershell
mkdir notebooks
# move the .ipynb you downloaded into notebooks\
```

Create `requirements.txt` listing packages by name:

```
google-genai
requests
beautifulsoup4
pydantic
jupyter
```

Install with the venv active:

```powershell
pip install -r requirements.txt
```

---

## Step 6 — Freeze exact versions

Names alone don't pin versions. Freeze the exact ones installed — the way a spec
calls out exact material grades, not just "steel":

```powershell
pip freeze > requirements.txt
```

---

## Step 7 — Inspect, then make your first commit

Always review before staging:

```powershell
git status
```

Confirm `.env` and `.venv/` do **not** appear. That is the real test that the
ignore file works. If they are correctly absent:

```powershell
git add .
git commit -m "Initial project skeleton: menu extraction POC notebook"
```

You now have a version-controlled, environment-isolated project with secrets
fenced off and zero secrets in history. The frame is up and it passed inspection.

---

## Post-commit check

Run `git status` after committing. It should report a clean tree, and `.env`
should be nowhere in what got committed.

**Next step:** connect this to a GitHub remote (same Pull Request workflow as
before), or port the notebook into a `src/` script.

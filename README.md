# Resume Tailor

Local CLI that tailors your LaTeX resume bullet points to a job description using local Claude Code CLI, then compiles a PDF. Your master resume is never modified.

## Requirements

1. **Python 3.11+**
2. A LaTeX distribution with `pdflatex` (MiKTeX or TeX Live)
3. Claude Code CLI installed and authenticated (`claude --help` should work)

## Setup

```bash
pip install -r requirements.txt
```

Optional: create a `.env` file in this directory to pin model:

```text
CLAUDE_MODEL=sonnet
```

## Usage

1. Put your master resume in `base_resume.tex` (or pass `--resume`).
2. Paste the job description into `job_description.txt`.
3. Run:

```bash
python tailor.py
```

Optional arguments:

```bash
python tailor.py --resume base_resume.tex --job job_description.txt --output output
```

## Output

Results are written to:

- `output/tailored_resume.tex`
- `output/tailored_resume.pdf`

The original `base_resume.tex` is never changed. Existing output files are moved to a timestamped backup folder before overwrite.

## How it works

1. Parses `\resumeItem{...}` bullets from your LaTeX.
2. Asks Claude to analyze the job description.
3. Asks Claude for replacement bullet text only (JSON) — not a full LaTeX rewrite.
4. Validates IDs, length, LaTeX safety, and factuality.
5. Splices approved text into a copy of your original LaTeX.
6. Compiles with `pdflatex` in a temporary directory.

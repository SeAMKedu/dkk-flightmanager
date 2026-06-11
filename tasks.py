import json
from invoke import task

SRC = "src/flightmanager"


@task
def lint(c):
    """Ruff: lint + complexity (C901, max 10)."""
    c.run(f"ruff check {SRC}")


@task
def cc(c):
    """Radon: cyclomatic complexity — show grade C and worse."""
    c.run(f"radon cc {SRC} -s -n C")


@task
def mi(c):
    """Radon: maintainability index — show grade B and worse."""
    c.run(f"radon mi {SRC} -s -n B")


@task
def loc(c):
    """Radon: raw line counts per file, sorted descending."""
    result = c.run(f"radon raw {SRC} --json", hide=True)
    data = json.loads(result.stdout)
    rows = sorted(data.items(), key=lambda x: x[1]["loc"], reverse=True)[:20]
    for path, metrics in rows:
        print(f"{metrics['loc']:>5}  {path}")


@task(pre=[lint, cc, mi, loc])
def check(c):
    """Run all checks."""

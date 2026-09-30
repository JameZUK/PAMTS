"""Shared assertion harness for the PAMTS test suites.

It exists because the suites drifted apart, and the drift caused real mistakes:

  * one suite printed "PASS", another printed "ok", so counting results with grep
    silently reported "0 checks" for a suite that was in fact passing
  * `test_promote.py` took (cond, msg) while every other suite took
    (name, cond, detail) -- and a mis-ordered call does not fail, it passes
    VACUOUSLY, because a non-empty name string is truthy

So `check()` validates its own arguments. A swapped call now raises instead of
quietly asserting nothing, which is the whole point: a test that cannot fail is
worse than no test, since it reports confidence it has not earned.
"""
import sys

_fails = []
_passes = []


class HarnessMisuse(TypeError):
    """Raised when check() is called with the wrong argument order."""


def check(name, cond, detail=""):
    """Assert `cond`, labelled `name`.

    `name` must be a string and `cond` must not be, which is what catches a
    swapped call. Deliberately strict: the alternative is a test that always
    passes.
    """
    if not isinstance(name, str):
        raise HarnessMisuse(
            f"check() takes (name, cond, detail); got name={type(name).__name__}. "
            "Arguments look swapped -- that would pass vacuously.")
    if isinstance(cond, str):
        raise HarnessMisuse(
            f"check({name!r}, ...) was given a string as the condition. "
            "A non-empty string is always truthy, so this would pass vacuously.")
    if cond:
        _passes.append(name)
        print(f"  ok   {name}")
    else:
        _fails.append(name)
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))
    return bool(cond)


def section(title):
    print(f"\n{title}")


def skip(reason, code=77):
    """Exit marking the suite as skipped. 77 is the conventional skip code, and
    is NOT a failure -- misreading it as one cost time once already."""
    print(f"SKIP: {reason}")
    sys.exit(code)


def summary():
    """Print the tally and exit non-zero if anything failed."""
    print()
    if _fails:
        print(f"{len(_fails)} FAILED of {len(_passes) + len(_fails)}: "
              f"{', '.join(_fails)}")
        sys.exit(1)
    print(f"all {len(_passes)} checks passed")
    sys.exit(0)


def counts():
    return len(_passes), len(_fails)

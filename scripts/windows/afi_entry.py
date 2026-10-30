"""Entry point for the packaged Windows build (Nuitka / PyInstaller compile this).

It is a plain top-level script, not part of the package, so the frozen program
has a clean main module. The package itself is pulled in via --include-package.
"""

from ai_fluency_insights.cli import main

if __name__ == "__main__":
    main()

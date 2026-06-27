"""Handler modules for VeluneREPL slash commands.

Each sub-module exports plain async functions that receive the REPL instance as
their first argument.  ``VeluneREPL._cmd_*`` methods are thin 2-line delegators
that call into these functions, keeping ``repl.py`` focused on lifecycle only.
"""

"""Non-UI service modules the TUI screens call into: pure I/O + logic, zero Textual.

:mod:`lode.tui.services.ask`, :mod:`lode.tui.services.capture`, :mod:`lode.tui.services.edit`,
:mod:`lode.tui.services.reconcile`, and :mod:`lode.tui.services.related` are grouped here by
kind, not by dependency layer — the split exists to stop these modules from shadowing their
same-named :mod:`lode.tui.screens` counterparts by name, not to impose a UI/logic hierarchy.
"""

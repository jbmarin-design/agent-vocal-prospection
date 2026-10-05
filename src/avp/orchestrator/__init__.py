"""Orchestrateur : import des prospects, planification et lancement des appels,
rapport hebdomadaire et tâches d'exploitation (purge, sauvegarde).

Les sous-modules n'importent ni `livekit` ni `anthropic` au chargement : les
dépendances lourdes (`avp.livekit_admin`, `avp.postcall`, client Anthropic) sont
importées paresseusement ou injectées, pour rester testables sans ces paquets.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def load_module(name: str) -> ModuleType:
    """Importe paresseusement `avp.<name>` (passe par `sys.modules`, donc remplaçable en test)."""
    return importlib.import_module(f"avp.{name}")

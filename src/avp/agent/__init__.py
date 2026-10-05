"""Worker vocal temps réel (LiveKit Agents).

- ``logic``   : logique pure et testable (état d'appel, issue par défaut, transcription,
                règles AMD/répondeur, persistance de fin d'appel). N'importe pas LiveKit.
- ``session`` : fabrique STT / LLM / TTS / VAD / détection de fin de tour depuis les Settings.
- ``agents``  : AgentAccueil et AgentDecideur, avec leurs outils.
- ``worker``  : point d'entrée ``python -m avp.agent.worker start|dev|console|download-files``.

Seuls ``session``, ``agents`` et ``worker`` importent ``livekit.agents`` / ``livekit.plugins``.
Ce fichier n'importe rien, pour que ``avp.agent.logic`` reste importable sans LiveKit.
"""

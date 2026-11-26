"""Internal transactional Realm and immutable-content substrate.

Import concrete services from their defining modules.  Keeping package import
side-effect free prevents low-level value records from pulling process/runtime
composition back into otherwise pure manifest code.
"""

__all__: list[str] = []

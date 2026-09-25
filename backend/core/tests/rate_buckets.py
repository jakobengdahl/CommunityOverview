"""Shared discovery of a ``SessionManager``'s rate buckets for the tests that
swap or snapshot every one of them.

Those tests replace each bucket with ``setattr`` on the manager, so they can
only cover a bucket held as an instance attribute. A bucket anywhere else a
reader of this code would look - on the class, or inside a container one level
down - would be skipped silently, and the test would keep passing while a call
charged it. This fails instead, so such a bucket forces the helper to grow.
"""

from backend.core.session_manager import _TokenBucket

REQUIRED_BUCKET_ATTRS = frozenset(
    {"_bucket", "_mcp_bucket", "_image_bucket", "_lookup_bucket"}
)

_CONTAINERS = (dict, list, tuple, set, frozenset)


def _members(value):
    if isinstance(value, dict):
        return [*value.keys(), *value.values()]
    return list(value)


def _stray_buckets(owner, namespace, *, direct):
    stray = []
    for name, value in namespace.items():
        if direct and isinstance(value, _TokenBucket):
            stray.append(f"{owner}.{name}")
        if isinstance(value, _CONTAINERS):
            stray.extend(
                f"{owner}.{name}[...]"
                for member in _members(value)
                if isinstance(member, _TokenBucket)
            )
    return stray


def bucket_attrs(manager):
    """The names of every ``_TokenBucket`` held as an instance attribute of
    ``manager``, after checking that none is held on its class (anywhere in
    the MRO) or one container level inside an instance or class attribute."""
    attrs = sorted(
        attr for attr, value in vars(manager).items() if isinstance(value, _TokenBucket)
    )
    assert REQUIRED_BUCKET_ATTRS <= set(attrs)

    stray = _stray_buckets("self", vars(manager), direct=False)
    for cls in type(manager).__mro__:
        stray += _stray_buckets(cls.__name__, vars(cls), direct=True)
    assert not stray, (
        f"rate buckets outside the manager's instance attributes: {stray}; "
        f"swapping instance attributes cannot cover them, so extend "
        f"bucket_attrs before relying on it"
    )
    return attrs

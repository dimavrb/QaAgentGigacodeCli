"""Synthetic application fragment; not a production implementation."""

def admin_users(role):
    # Intentionally faulty: role is ignored.
    return 200, {"users": ["fixture-user"], "branch": "list_users_without_role_check"}

import os


# Unit and integration tests must never depend on a developer's untracked .env.
os.environ.setdefault(
    "JWT_SECRET_KEY", "automated-test-only-secret-at-least-32-bytes"
)

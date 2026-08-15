"""Carry chat history over from the retired per-provider apps.

Before the OpenRouter migration every handler wrote to `gpt_handler_historyprompt`.
That app is gone, so copy its rows into the new table once. The old table is left
untouched on disk as a backup — drop it manually when you are happy with the result.
"""

from django.db import migrations

LEGACY_TABLE = "gpt_handler_historyprompt"
NEW_TABLE = "openrouter_handler_historyprompt"


def copy_legacy_history(apps, schema_editor):
    connection = schema_editor.connection

    if LEGACY_TABLE not in connection.introspection.table_names():
        return  # Fresh install: nothing to carry over.

    HistoryPrompt = apps.get_model("openrouter_handler", "HistoryPrompt")
    if HistoryPrompt.objects.exists():
        return  # Already populated; do not duplicate rows.

    with connection.cursor() as cursor:
        legacy_columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, LEGACY_TABLE
            )
        }
    # `conversation_id` only exists if gpt_handler migration 0002 had been applied.
    conversation_expr = (
        '"conversation_id"' if "conversation_id" in legacy_columns else "NULL"
    )

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO "{NEW_TABLE}"
                ("prompt", "response", "conversation_id", "model_name",
                 "created_at", "updated_at")
            SELECT "prompt", "response", {conversation_expr}, "model_name",
                   "created_at", "updated_at"
            FROM "{LEGACY_TABLE}"
            """
        )


def noop(apps, schema_editor):
    """Reversing leaves the copied rows in place; the legacy table still has them."""


class Migration(migrations.Migration):
    dependencies = [
        ("openrouter_handler", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(copy_legacy_history, noop),
    ]

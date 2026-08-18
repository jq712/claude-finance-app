class ToolInputError(ValueError):
    """Raised by a tool handler when arguments fail validation.

    The agent loop catches this and turns it into a structured error result
    the model can see and react to — never an unhandled exception that
    crashes the conversation, and never a path that reaches the database
    with unvalidated input.
    """

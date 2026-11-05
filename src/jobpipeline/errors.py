"""Exception hierarchy.

Every error raised on purpose by the pipeline inherits from ``PipelineError``, so the
entry point can tell expected failures (bad config, API down) from real bugs.
"""


class PipelineError(Exception):
    """Base class for all expected pipeline errors."""


class ConfigError(PipelineError):
    """The configuration file or environment variables are missing or invalid."""


class SourceError(PipelineError):
    """A job source (France Travail, Adzuna) failed to return offers."""


class LLMError(PipelineError):
    """The Claude API call failed or returned an unusable response."""


class DocumentError(PipelineError):
    """A .docx document could not be rendered."""


class NotionError(PipelineError):
    """A Notion API call failed."""

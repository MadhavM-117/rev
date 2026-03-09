from .base import BaseDiffer, GitRichDiffer


def run_diff(ref=None) -> None:
    differ = GitRichDiffer()
    differ.pretty_print(ref=ref)


__all__ = ["BaseDiffer", "GitRichDiffer", "run_diff"]

from git import Repo


class GitDiffSource:
    def get_diff(self, ref=None) -> str:
        repo = Repo(search_parent_directories=True)
        return repo.git.diff() if ref is None else repo.git.diff(ref)

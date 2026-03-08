from git import Repo


def run_diff(ref=None):
    d = _Differ()
    d.pretty_print(ref)


class _Differ:
    """
    Internal helper class to interact with git diffs
    """

    def pretty_print(self, ref=None):
        """
        Display the git diff
        """
        repo = Repo(search_parent_directories=True)
        if ref is None:
            print(repo.git.diff())
            return

        print(repo.git.diff(ref))

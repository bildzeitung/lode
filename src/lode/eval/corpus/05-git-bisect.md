# Finding a regression with git bisect

git bisect does a binary search through commit history to find the commit that
introduced a bug. You start it with "git bisect start", mark a known-bad commit
with "git bisect bad", and a known-good commit with "git bisect good". Git then
checks out a commit halfway between them.

At each step you test the checked-out commit and tell git the result with "git
bisect good" or "git bisect bad". Each answer halves the remaining range, so a
thousand commits are narrowed down in about ten steps. When it finishes, git
prints the first bad commit.

If you have a script that exits zero for good and non-zero for bad, "git bisect
run ./test.sh" automates the whole search. Always finish with "git bisect reset"
to return to the branch you started on.

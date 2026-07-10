# Feedback on current work

This document presents a list of feedback items. 

Create a P0 epic to track the items in this spec.

Evaluate each item and,

- ask to disambiguate any concerns / uncertainties
- file tickets as needed under the new epic

## Test optimisation

- unit tests have grown significantly and take a long time to run
- make a matrix of features that each test covers
- weigh the value of each test
- return a list of the importance of the tests, ranked
- consider a cutoff ranking where tests under that cutoff could be removed
- consider tests that duplicate coverage and flag them for deletion
- consider tests that are redundant and flag them for deletion
- consider if some of the tests are too granular and if it's possible to combine them with a small code change
- create a series of checklists of the results so that a future /code pass will address each item


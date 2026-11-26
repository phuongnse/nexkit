"""The scenario families of the differential check, each a module with `scenarios()`, which
yields each scenario as plain JSON in a fixed order, and `run(sc)`, which plays one on a fresh
`FakeGitHub` of the tree being played and returns what it recorded. A family's scenarios
depend on nothing of the tree, so both trees play the same ones.

A change that moves a decision into the engine adds a family for it here, by the pull request
that adds it."""

# Each family, by the pull request whose decisions it plays.
FAMILIES = {
    "report": "#341",
    "stop": "#341",
    "sweep": "#344",
    "round": "#344",
    "required": "#344",
    "give": "#344",
    "diag": "#347",
    "many": "#347",
    "conflict": "#347",
    "review": "#347",
    "chains": "#347",
    "released": "#347",
    "waits": "#390",
    "refusals": "#391",
    "pause": "#398",
    "ended": "#393",
    "failure": "#415",
    "lost": "#399",
    "replan": "#413",
    "conflicted": "#423",
}

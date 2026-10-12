# Onboarding worker: glossary

The words below have one meaning each across the code, the design notes and the conversations about them.

## Skill

A versioned folder of data and know-how that the worker reads and that people can improve through approved changes: when to use it, what to ask, what to show, decision tables, worked cases. A skill never contains code. Anything it needs executed is an operation of the engine that it names.

## Engine

Generic, versioned code that evaluates skill data to produce a result: select, group, sum, compare, decide, write. It holds no business constants of its own. A new kind of operation is an engineer's change, never an improvement proposal.

## Control

A rule that decides what passes without anyone looking: a tolerance, a severity level, the balance rule, a required-field floor, an identifier format, the rule that a missing value stays blank and is never invented. If it is wrong or loosened, a bad number goes through unseen. A control is bounded by limits fixed in code and changes only through a controller. Some rules are one-way: a proposal may make the checking stricter, for example adding an account to the list that needs a deal, but loosening it needs a controller.

## Controller

A named person who may change a control. The controller is never the person who proposed the change.

## Proposal

A suggested change to a skill, drafted from feedback: the smallest edit to one file, the cases that triggered it, and what replay predicts. A proposal changes nothing until a person approves it. A proposal to change a control is drafted like any other but can only be approved by a controller.

## Replay

Running recorded jobs again under a candidate skill version and comparing the result with what was produced and approved. Replay shows whether a proposal is harmless on the recorded cases. It cannot show that a change to a control is acceptable.

## Pin

The exact content of a skill (its folder plus any sponsor overlay) in force when a job ran, identified by a checksum and recorded on the job and on every audit entry. A pinned job can be reproduced later.

## Target

The Ledgerline upload templates and their rules: the fixed, known contract. Everything the worker builds is checked against it. The source side is unknown and open-ended.

## Recipe

A user-authored instruction that turns an unknown source column or format into a target field, using a closed set of safe transforms. A recipe belongs to one sponsor and record type and applies once the user approves it. Each approved recipe is also recorded as feedback and can, through a proposal, improve the shared skills for everyone.

## Sponsor overlay

Sponsor-specific adjustments to learnable skill data.

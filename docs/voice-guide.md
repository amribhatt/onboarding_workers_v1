# Voice guide: how the worker talks to you

The worker is one AI assistant. The person it talks to is signed in, so it knows who they are. Everything the
console, the chat and the model say follows these rules. `tests/test_voice.py` checks the ones a machine can check.

1. **I and you.** The worker says "I" for itself and "you" for the person. Labels that name it say "me" or "my"
   ("Tell me what you need", "I use the model in the first row"). The chat shows one speaker, "Worker"; which part of
   the worker answered (checker, mapper ...) is not shown as another speaker.
2. **Never a role word for you.** Not "the user", "the human", "the person", "the analyst", "the reviewer", "a named
   person", "the worker" or "the system" in anything shown. Where a decision needs you, say what to do: "Choose how
   Investors IDs are made before you can approve", not "A named person chooses ...".
3. **Records name the real person.** The audit, the timeline and the files carry the signed-in name and email
   (`Name <email>`). They do not say "the analyst" or "the person"; a value typed in by hand is "supplied by hand".
4. **A refusal or a failure has one shape:** what happened, in a full sentence, then "Nothing changed." When I could not
   write the record of an action: "I couldn't record the decision, so nothing changed: <reason>." When I did not write
   the file: "I didn't write the file, so the job is not approved: <reason>."
5. **Sentences.** A capital letter and a full stop. No lowercase fragments in anything shown.
6. **The model speaks this way too.** `role_runner.COMMON_RULES` and `prompts._BODY` carry the same rule, so a reply
   the model writes says "I" and "you".

Where the words "person", "human" or "named" still appear it is for a reason: the rule that an agent can never
approve its own work (`contracts`, `engine`, `agentspec`, `improver`), the instructions that are for the model and not
for you, and a check that a name is a real name ("isn't a person's name"). Those are not shown to you as speech.

"""What a turn's tool loop (EntityRuntime._run_tool_loop, runtime/session.py)
shares with the rest of the turn (_send_message_core): values the loop sets
that the helpers the core hands it (_go_ahead_round, _on_resume, _keep_reply,
_continue_card, the reply recorder's getters result and ct_reply_result) read
or set too, and those the core reads after the loop.

They live on one object, `turn`, made once per turn and handed to the loop, so
the loop and the rest of the turn always read the same value: the loop is a
method of its own, and a value it kept in a variable would be its own copy. A
value read before the turn sets it raises AttributeError, as reading the unset
variable did.
"""


class TurnState:
    __slots__ = (
        # The latest model call's response: the loop's, or the go-ahead
        # repair's own call.
        "result",
        # Tokens the turn's model calls used, summed: in all, prompt, completion.
        "total_tokens",
        "_prompt_acc",
        "_completion_acc",
        # The allowance ended the tool loop while a cut reply waited:
        # "text" or "empty"; False otherwise.
        "_spend_cut_reply",
        # The close_turn signal the Host sent (the first one is the turn's),
        # or None.
        "_close_turn_signal",
        # close_turn's writes have run (once per turn).
        "_ct_written",
        # The next result is the go-ahead repair's own answer.
        "_ga_awaiting",
        # The handoff close_turn recorded no longer says where the work
        # stands (after a go-ahead repair, or a resume after a hold).
        "_ct_handoff_stale",
        # The model call that owes close_turn's reply (a tool call in it is
        # not run), or None.
        "_ct_reply_result",
        # Rounds of tool calls the loop has run, from 0 (words the person
        # sends during the turn start it again). The round cap reads it, in
        # the loop and after it.
        "tool_round",
        # The allowance ended the tool loop.
        "_spend_stopped",
        # The one retry after an empty reply ran (inside the loop, with the
        # turn's tools).
        "_empty_retried",
        # The loop left because the turn was closed.
        "_ct_left_closed",
    )

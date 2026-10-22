---
name: world
description: Your own database, for data you build for a task (a meal plan, a tracker, a list the work produces), in tables you design in your schema `world`. What the person tells you about themselves is memory, not a table. How to build, read, describe and restore it with the `data` tool.
allowed-tools: data, memory_curate
---

# Your own database

## When to use

- You are building data for a task: a meal plan, a tracker, a schedule, a list the work produces or keeps. A table holds its shape, and you can extend it later.
- Not for what the person tells you about themselves (how they live, what they own, what they like): that is memory, kept with `close_turn`'s preferences or `memory_curate`, and recall brings it back. A task's table may use those facts; it does not replace them.
- You need to answer a question from your own records (conversations, memory, tool log) and recall did not find it. Your own tables in `public` are readable with SQL.
- You keep answering the same question by hand. Write a view or function in `world` that answers it, and it becomes a tool: `find_tools` lists it as `world:<name>`.

## Steps

1. **Look first.** `read(tool='data', args={op:'describe'})` lists what is already in `world`. Recall and the frame also show tables that match the conversation. Extend what exists before making something new.
2. **Design it.** Small tables with clear columns. A primary key. A `COMMENT` on every table, view and function saying what it is for, in the person's terms. Column comments where the meaning is not obvious.
3. **Build and fill it in one call.** For example:

   ```sql
   CREATE TABLE packing_list (
       item  text PRIMARY KEY,
       qty   int  NOT NULL DEFAULT 1,
       note  text
   );
   COMMENT ON TABLE packing_list IS 'What to pack for the June hiking trip, and how many of each.';
   INSERT INTO packing_list (item, qty) VALUES ('rain jacket', 1), ('wool socks', 4);
   ```

   Run it with `operate(tool='data', args={op:'sql', sql:'...'}, intent='...')`. Unqualified names land in `world`.
4. **Read with parameters.** One statement, values as strings: `operate(tool='data', args={op:'sql', sql:'SELECT item FROM packing_list WHERE qty > $1', params:['2']}, intent='...')`. They are converted to the column types.
5. **Make a tool when it pays.** A function with a `COMMENT` is callable as `operate(tool='world:<name>', args={...}, intent='...')`.

## Checks

- The call returns `status: ok`. A table, view or function you made without a `COMMENT` rolls the whole call back, and the answer gives the exact statement to add.
- `read(tool='data', args={op:'describe', name:'packing_list'})` shows the object, its purpose, its columns and its row estimate.

## When it fails

- `no_purpose`: add the `COMMENT ON ...` line the answer gives, in the same call, and run it again.
- `sql_error` with SQLSTATE 42501: you tried to write outside `world`. Your records in `public` are read-only. Memory changes go through `memory_curate`.
- `timeout`: pass `timeout_s` for a long job (up to the person's limit
  `host_data.query_max_minutes`, 15 minutes unless they changed it; skill
  'limits'), or narrow the query.
- You changed or dropped something by mistake: `operate(tool='data', args={op:'history'}, intent='...')` lists snapshots, taken before any change that can remove data. `operate(tool='data', args={op:'restore', snapshot:'<id>'}, intent='...')` puts `world` back, and gives you an undo snapshot.

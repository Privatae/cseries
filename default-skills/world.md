---
name: world
description: Your own database. When the person tells you something with a shape of its own (a household, a kitchen, a routine, a collection), store it in tables you design in your schema `world`, so you can find it and use it later. How to build, read, describe and restore it with the `data` tool.
allowed-tools: data, memory_curate
---

# Your own database

## When to use

- The person tells you facts that have a shape: things they own, how they live, what they do on a schedule, lists they keep. A preference note holds a sentence. A table holds the shape.
- You need to answer a question from your own records (conversations, memory, tool log) and recall did not find it. Your own tables in `public` are readable with SQL.
- You keep answering the same question by hand. Write a view or function in `world` that answers it, and it becomes a tool: `find_tools` lists it as `world:<name>`.

## Steps

1. **Look first.** `read(tool='data', args={op:'describe'})` lists what is already in `world`. Recall and the frame also show tables that match the conversation. Extend what exists before making something new.
2. **Design it.** Small tables with clear columns. A primary key. A `COMMENT` on every table, view and function saying what it is for, in the person's terms. Column comments where the meaning is not obvious.
3. **Build and fill it in one call.** For example:

   ```sql
   CREATE TABLE kitchen (
       tool  text PRIMARY KEY,
       uses  int  NOT NULL DEFAULT 0,
       note  text
   );
   COMMENT ON TABLE kitchen IS 'The cooking tools Alex has, and how often Alex uses each.';
   INSERT INTO kitchen (tool) VALUES ('air fryer'), ('rice cooker');
   ```

   Run it with `operate(tool='data', args={op:'sql', sql:'...'}, intent='...')`. Unqualified names land in `world`.
4. **Read with parameters.** One statement, values as strings: `operate(tool='data', args={op:'sql', sql:'SELECT tool FROM kitchen WHERE uses > $1', params:['2']}, intent='...')`. They are converted to the column types.
5. **Make a tool when it pays.** A function with a `COMMENT` is callable as `operate(tool='world:<name>', args={...}, intent='...')`.

## Checks

- The call returns `status: ok`. A table, view or function you made without a `COMMENT` rolls the whole call back, and the answer gives the exact statement to add.
- `read(tool='data', args={op:'describe', name:'kitchen'})` shows the object, its purpose, its columns and its row estimate.

## When it fails

- `no_purpose`: add the `COMMENT ON ...` line the answer gives, in the same call, and run it again.
- `sql_error` with SQLSTATE 42501: you tried to write outside `world`. Your records in `public` are read-only. Memory changes go through `memory_curate`.
- `timeout`: pass `timeout_s` (up to 900) for a long job, or narrow the query.
- You changed or dropped something by mistake: `operate(tool='data', args={op:'history'}, intent='...')` lists snapshots, taken before any change that can remove data. `operate(tool='data', args={op:'restore', snapshot:'<id>'}, intent='...')` puts `world` back, and gives you an undo snapshot.

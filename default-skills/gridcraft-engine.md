---
name: gridcraft-engine
description: How to build spreadsheets with the GridCraft Engine app — entering values and formulas, formatting, tables, charts, sorting and filtering, reading results back, and saving XLSX, CSV or HTML. Present when the person has installed GridCraft Engine.
present_if_app_installed: gridcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# GridCraft Engine

GridCraft Engine runs GridCraft, an open-source spreadsheet in the style of
Excel, headless in the person's own space, for you. It is an MCP server named
`gridcraft-engine` (skill 'mcp', "An installed app's own server"): connected
when the app was installed, nothing to approve. Workbooks stay open in the
app between calls.

## When to use

Budgets, trackers, tables of data with formulas and charts, cleaning up or
analysing the person's own XLSX or CSV files, and handing them back as XLSX
or CSV.

## First time

1. `mcp_connections(op='tools', name='gridcraft-engine')`: it wakes the app.
   The tools are GridCraft's own (`new_workbook`, `open_workbook`,
   `write_range`, `set_cell`, `read_range`, `get_cell`, `evaluate_formula`,
   `format_range`, `insert_chart`, `create_table`, `sort_range`, `filter`,
   `execute_command`, `list_commands`, `list_functions`, `inspect_workbook`,
   `save_workbook`, `undo`, `redo`) and the app's `file_put`, `file_get`,
   `file_list`.
2. Write `skills/mcp-gridcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-gridcraft-engine', content:…})`.

## Steps

1. **A workbook:** `new_workbook()`, or `file_put` the person's file into `in/`
   and `open_workbook(path: 'in/<name>.xlsx')` (.xlsx, .xlsm, .xlsb, .ods,
   .csv, .tsv, .txt, .json).
2. **Values and formulas:** `write_range(range: 'A1', values: [['Month','Revenue','Costs','Profit'], ['Jan', 12000, 8000, '=B2-C2'], …])`.
   A string starting with `=` is a formula; other strings are read as typed
   (`"12%"`, `"1/2/2024"`). One cell: `set_cell(cell: 'F1', input: '=SUM(D2:D7)')`.
   Tens of thousands of rows go in one call.
3. **Format:** `format_range(range: 'A1:E1', bold: true, fillColor: '#1F4E79', fontColor: '#FFFFFF')`,
   `format_range(range: 'B2:D8', numberFormat: '#,##0')`, `numberFormat: '0.0%'`,
   `borders: 'topDoubleBottom'`.
4. **Chart:** `insert_chart(range: 'A1:C7', type: 'column', subtype: 'clustered', title: 'Revenue and costs', at: 'G2')`;
   the first row and column name the series and categories. Types: column, bar,
   line, pie, doughnut, area, scatter, and more. `execute_command(command: 'chart.set', params: {chart, legend, xTitle, yTitle})`
   changes one.
5. **Read back:** `read_range(range: 'A1:E8')` gives computed values,
   `formatted: true` the text as shown, `formulas: true` the formulas.
   `evaluate_formula(formula: '=AVERAGE(D2:D7)')` tries a formula without
   changing the sheet.
6. **Save:** `save_workbook(path: 'sheets/<name>.xlsx')`. With `.csv` it saves
   the active sheet's values, with `.html` a web page of the sheet. Save the
   `.xlsx` too: formulas, formats and charts live only there.
7. **Anything else:** every ribbon action is a command:
   `list_commands(search: 'sort')`, then `execute_command(command, params)`.

## Checks

- `inspect_workbook` lists sheets, used ranges, tables, charts (kind, series,
  title) and whether there are unsaved changes (`dirty`).
- There is no picture of the sheet: check numbers with `read_range` and
  `get_cell`, and the chart with `inspect_workbook`. The person sees the
  saved files in the app's window.

## When it fails

- **`open_workbook` answers `alreadyOpen: true`**: that file is already open,
  and the open copy is used; it was not read again from disk.
- A formula error comes back in `read_range` as `{"error": "#DIV/0!"}`.

## Reference

GridCraft is MIT or Apache-2.0 (github.com/storytold/gridcraft).

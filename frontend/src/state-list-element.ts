import { LitElement, css, html } from "lit";
import { property } from "lit/decorators.js";
import { keyed } from "lit/directives/keyed.js";
import { repeat } from "lit/directives/repeat.js";
import { paletteCss } from "./colors";
import {
  automaticIndex,
  rowsAfterEntityChange,
  type StateList,
  type StateRow,
} from "./state-list";
import type { HassLike } from "./types";

// mdi:drag-horizontal-variant, the handle the stock row editors use.
const DRAG_ICON = "M21 11H3V9H21V11M21 13H3V15H21V13Z";
const AUTO = "auto";
// The switch `ha-form` draws for its own boolean fields, so the tick under
// the list looks like the ones beside it.
const IGNORE_NEW_SELECTOR = { boolean: {} };
// The theme colours plus an "automatic" entry, drawn in the colour the
// chart would give the row so the picker says what automatic means. A row
// with no colour hands the picker no value, so it shows that default
// without the clear button a chosen colour gets.
const colorSelector = (automatic: string) => ({
  ui_color: {
    default_color: AUTO,
    extra_options: [{ value: AUTO, label: "Automatic", display_color: automatic }],
  },
});
// The entity field the editor's own form draws, limited the same way.
const entitySelector = (entities?: string[]) => ({
  entity: entities ? { include_entities: entities } : {},
});
const stateSelector = (options: { value: string; label: string }[]) => ({
  select: { mode: "dropdown", options },
});

// One row per state: drag handle, drawn tick, name — the stored one as the
// placeholder, so a row reads the same until it is renamed — and colour.
// Nothing here needs importing: `ha-sortable` comes with every dashboard
// view, `ha-input` with every `ha-form`, and `ha-selector` fetches the
// ui_color selector on first use. Changes go out as a whole new list
// through `value-changed`; the editor turns it into config.
export class DiscreteStatisticsStateList extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @property({ attribute: false }) public value?: StateList;

  // In entities mode only: the entities a row may name, and the states each
  // of them has statistics for, keyed by entity ID.
  @property({ attribute: false }) public entities?: string[];

  @property({ attribute: false }) public stateOptions?: Record<
    string,
    { value: string; label: string }[]
  >;

  protected render() {
    const list: StateList = this.value ?? {
      rows: [],
      ignoreNew: false,
      mode: "states",
    };
    const multi = list.mode === "entities";
    return html`
      <ha-sortable handle-selector=".handle" @item-moved=${this._rowMoved}>
        <div class="rows">
          ${repeat(
            list.rows,
            // Two rows may name the same entity and state, so only the index is unique.
            (row, index) => (multi ? index : row.token || row.label),
            (row, index) => html`
              <div class="row ${multi ? "multi" : ""}">
                <div class="handle">
                  <ha-svg-icon .path=${DRAG_ICON}></ha-svg-icon>
                </div>
                ${multi
                  ? html`
                      <ha-selector
                        class="entity"
                        .hass=${this.hass}
                        .selector=${entitySelector(this.entities)}
                        .value=${row.entity}
                        .index=${index}
                        @value-changed=${this._entityChanged}
                      ></ha-selector>
                      <ha-selector
                        class="state"
                        .hass=${this.hass}
                        .selector=${stateSelector(this._optionsFor(row))}
                        .value=${row.token}
                        .index=${index}
                        @value-changed=${this._stateChanged}
                      ></ha-selector>
                    `
                  : html`
                      <ha-checkbox
                        .checked=${row.shown}
                        .index=${index}
                        @change=${this._shownChanged}
                      ></ha-checkbox>
                    `}
                <ha-input
                  class="name"
                  .placeholder=${row.label}
                  .value=${row.name ?? ""}
                  .index=${index}
                  @change=${this._nameChanged}
                ></ha-input>
                <ha-selector
                  .hass=${this.hass}
                  .selector=${colorSelector(paletteCss(automaticIndex(list.rows, index)))}
                  .value=${row.color}
                  .index=${index}
                  @value-changed=${this._colorChanged}
                ></ha-selector>
              </div>
            `
          )}
        </div>
      </ha-sortable>
      ${multi
        ? // A picker per row count: the entity it took would otherwise stay
          // in it once the row is drawn above.
          keyed(
            list.rows.length,
            html`<ha-selector
              class="add"
              .hass=${this.hass}
              .selector=${entitySelector(this.entities)}
              .index=${list.rows.length}
              @value-changed=${this._entityChanged}
            ></ha-selector>`
          )
        : html`<ha-selector
            .hass=${this.hass}
            .selector=${IGNORE_NEW_SELECTOR}
            .label=${"Ignore states that appear later"}
            .value=${list.ignoreNew}
            @value-changed=${this._ignoreNewChanged}
          ></ha-selector>`}
    `;
  }

  // An unlisted state is offered as itself, so a row the statistics do not
  // know reads as what it is rather than blank.
  private _optionsFor(row: StateRow): { value: string; label: string }[] {
    const options = this.stateOptions?.[row.entity ?? ""] ?? [];
    if (!row.token || options.some((option) => option.value === row.token)) {
      return options;
    }
    return [...options, { value: row.token, label: row.token }];
  }

  private _entityChanged(ev: CustomEvent<{ value?: string }>) {
    ev.stopPropagation();
    const target = ev.currentTarget as HTMLElement & { index: number };
    const rows = rowsAfterEntityChange(this.value!.rows, target.index, ev.detail.value);
    if (rows !== this.value!.rows) {
      this._announce({ ...this.value!, rows });
    }
  }

  private _stateChanged(ev: CustomEvent<{ value?: string }>) {
    ev.stopPropagation();
    const target = ev.currentTarget as HTMLElement & { index: number };
    this._updateRow(target.index, { token: ev.detail.value ?? "" });
  }

  private _rowMoved(ev: CustomEvent<{ oldIndex: number; newIndex: number }>) {
    ev.stopPropagation();
    const rows = [...this.value!.rows];
    rows.splice(ev.detail.newIndex, 0, rows.splice(ev.detail.oldIndex, 1)[0]);
    this._announce({ ...this.value!, rows });
  }

  private _shownChanged(ev: Event) {
    const target = ev.currentTarget as HTMLElement & { index: number; checked: boolean };
    this._updateRow(target.index, { shown: target.checked });
  }

  private _nameChanged(ev: Event) {
    const target = ev.currentTarget as HTMLElement & { index: number; value: string };
    const name = target.value.trim();
    this._updateRow(target.index, { name: name || undefined });
  }

  private _colorChanged(ev: CustomEvent<{ value?: string }>) {
    ev.stopPropagation();
    const target = ev.currentTarget as HTMLElement & { index: number };
    const value = ev.detail.value;
    this._updateRow(target.index, {
      color: value && value !== AUTO ? value : undefined,
    });
  }

  private _ignoreNewChanged(ev: CustomEvent<{ value: boolean }>) {
    ev.stopPropagation();
    this._announce({ ...this.value!, ignoreNew: ev.detail.value });
  }

  private _updateRow(index: number, change: Partial<StateRow>) {
    const rows = this.value!.rows.map((row, i) =>
      i === index ? { ...row, ...change } : row
    );
    this._announce({ ...this.value!, rows });
  }

  private _announce(value: StateList) {
    this.value = value;
    this.dispatchEvent(
      new CustomEvent("value-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
  }

  static styles = css`
    .rows {
      display: flex;
      flex-direction: column;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .handle {
      cursor: grab;
      color: var(--secondary-text-color);
      display: flex;
    }
    .handle > * {
      pointer-events: none;
    }
    .name {
      flex: 1;
      min-width: 0;
      /* ha-input pads below itself for helper text; the picker beside
         it does not, so the pad would lift the field off the row's
         centre line. */
      --ha-input-padding-bottom: 0;
    }
    ha-selector {
      width: 180px;
    }
    .row.multi {
      flex-wrap: wrap;
    }
    ha-selector.entity {
      flex: 1;
      min-width: 0;
      width: auto;
    }
    ha-selector.add {
      display: block;
      width: auto;
      margin-top: 8px;
    }
    ha-selector.state {
      width: 140px;
    }
  `;
}

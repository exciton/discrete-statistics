import { LitElement, css, html } from "lit";
import { property } from "lit/decorators.js";
import { repeat } from "lit/directives/repeat.js";
import { paletteCss } from "./colors";
import { automaticIndex, type StateList, type StateRow } from "./state-list";
import type { HassLike } from "./types";

// mdi:drag-horizontal-variant, the handle the stock row editors use.
const DRAG_ICON = "M21 11H3V9H21V11M21 13H3V15H21V13Z";
// The frontend's theme colours plus an entry for the palette, which is
// what a row has until a colour is picked - shown in the colour the
// chart would give that row, so the picker says what "automatic" means.
// A row without a colour hands the picker no value, so it shows that
// default without the clear button a chosen colour gets.
const AUTO = "auto";
// The switch the form draws for its own boolean fields, so the tick under
// the list looks like the one beside it.
const IGNORE_NEW_SELECTOR = { boolean: {} };
const colorSelector = (automatic: string) => ({
  ui_color: {
    default_color: AUTO,
    extra_options: [{ value: AUTO, label: "Automatic", display_color: automatic }],
  },
});

// One row per state: a drag handle, a tick for whether it is drawn, its
// name — the stored one as the placeholder, so a row reads the same
// until it is renamed — and a colour. `ha-sortable` is the frontend's own, which every
// dashboard view loads; `ha-input` is what every `ha-form` text field is,
// so the editor dialog has it; a ui_color selector is fetched by
// `ha-selector` on first use — so none needs importing here. Changes are announced
// as a whole new list through `value-changed`; the editor turns it into
// config.
export class DiscreteStatisticsStateList extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @property({ attribute: false }) public value?: StateList;

  protected render() {
    const list = this.value ?? { rows: [], ignoreNew: false };
    return html`
      <ha-sortable handle-selector=".handle" @item-moved=${this._rowMoved}>
        <div class="rows">
          ${repeat(
            list.rows,
            (row) => row.token || row.label,
            (row, index) => html`
              <div class="row">
                <div class="handle">
                  <ha-svg-icon .path=${DRAG_ICON}></ha-svg-icon>
                </div>
                <ha-checkbox
                  .checked=${row.shown}
                  .index=${index}
                  @change=${this._shownChanged}
                ></ha-checkbox>
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
      <ha-selector
        .hass=${this.hass}
        .selector=${IGNORE_NEW_SELECTOR}
        .label=${"Ignore states that appear later"}
        .value=${list.ignoreNew}
        @value-changed=${this._ignoreNewChanged}
      ></ha-selector>
    `;
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
  `;
}

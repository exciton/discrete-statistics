import { LitElement, html, css } from "lit";
import { property, state } from "lit/decorators.js";
import type { CardConfig, HassLike } from "./types";

export class DiscreteStatisticsCard extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @state() private _config?: CardConfig;

  public setConfig(config: CardConfig): void {
    if (!config.entity) {
      throw new Error("entity is required");
    }
    this._config = config;
  }

  public getCardSize(): number {
    return 5;
  }

  protected render() {
    if (!this._config) {
      return html``;
    }
    return html`<ha-card .header=${this._config.title ?? ""}>
      <div class="content">${this._config.entity}</div>
    </ha-card>`;
  }

  static styles = css`
    .content {
      padding: 16px;
    }
  `;
}

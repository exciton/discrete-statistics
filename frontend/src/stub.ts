// The picker hands a new card the dashboard's unused entities, then every
// entity; the stub config takes the first whose domain this integration
// is for, so the preview draws something instead of failing on a missing
// entity.
const DOMAINS = [
  "binary_sensor",
  "switch",
  "light",
  "cover",
  "climate",
  "input_boolean",
  "person",
  "device_tracker",
  "vacuum",
  "water_heater",
  "lock",
  "fan",
];

export function pickStubEntity(entities: string[], fallback: string[]): string | undefined {
  for (const list of [entities, fallback]) {
    for (const domain of DOMAINS) {
      const found = list.find((id) => id.startsWith(`${domain}.`));
      if (found) {
        return found;
      }
    }
  }
  return undefined;
}

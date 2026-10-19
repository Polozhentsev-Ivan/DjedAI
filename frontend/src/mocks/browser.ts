import { createHandlers, type Scenario } from './handlers';

const SCENARIOS: readonly Scenario[] = ['normal', 'empty', 'slow', 'error'];
const KEY = 'djedai.mock';

/** `?mock=<scenario>` sticks for the tab, so in-app navigation keeps it; `?mock=normal` resets. */
function readScenario(): Scenario {
  const fromUrl = new URLSearchParams(window.location.search).get('mock');
  try {
    if (fromUrl) window.sessionStorage.setItem(KEY, fromUrl);
    const value = fromUrl ?? window.sessionStorage.getItem(KEY);
    return SCENARIOS.find((s) => s === value) ?? 'normal';
  } catch {
    return SCENARIOS.find((s) => s === fromUrl) ?? 'normal';
  }
}

/** Starts the in-browser mock backend (MSW). Called only in dev when mocks are on. */
export async function startMocks(): Promise<void> {
  const { network } = await import('virtual:msw');
  network.configure({ handlers: createHandlers({ scenario: readScenario }), onUnhandledFrame: 'bypass' });
  await network.enable();
  console.info(`[mock api] сценарий: ${readScenario()} — переключается параметром ?mock=normal|empty|slow|error`);
}

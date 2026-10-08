import sparticuz from '@sparticuz/chromium';
export async function launch(pw, extra = []) {
  const exec = await sparticuz.executablePath();
  return pw.launch({
    executablePath: exec,
    args: [...sparticuz.args, '--no-sandbox', '--disable-gpu', '--font-render-hinting=none',
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
      '--allow-autoplay', ...extra],
    headless: true,
    env: { ...process.env, LD_LIBRARY_PATH: '/tmp/al2023/lib:/tmp:/tmp/sw',
      FONTCONFIG_PATH: '/tmp/fonts', VK_ICD_FILENAMES: '/tmp/sw/vk_swiftshader_icd.json' },
  });
}

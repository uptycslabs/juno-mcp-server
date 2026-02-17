/**
 * Minimal ECharts SSR renderer.
 *
 * Reads a JSON ECharts option from stdin, renders to SVG,
 * writes the SVG string to stdout.
 *
 * Usage:  echo '{"xAxis":...}' | node echart_render.js [width] [height]
 */

const echarts = require("echarts");

const width = parseInt(process.argv[2], 10) || 600;
const height = parseInt(process.argv[3], 10) || 400;

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  try {
    const option = JSON.parse(input);
    const chart = echarts.init(null, null, {
      renderer: "svg",
      ssr: true,
      width,
      height,
    });
    chart.setOption(option);
    process.stdout.write(chart.renderToSVGString());
    chart.dispose();
  } catch (err) {
    process.stderr.write(err.message + "\n");
    process.exit(1);
  }
});

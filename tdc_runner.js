// Only run inside the networkless, nonroot Docker worker. vm is NOT a sandbox.
'use strict';
const fs = require('fs');
const vm = require('vm');
try {
  const context = vm.createContext({});
  vm.runInContext('window=this;self=this;', context, {timeout: 1500});
  vm.runInContext(fs.readFileSync('/tdc.js', 'utf8'), context, {timeout: 1500});
  const result = vm.runInContext(`(() => {
    const collect=decodeURIComponent(window.TDC.getData(true));
    const info=window.TDC.getInfo()||{};
    return {collect,tlg:collect.length,eks:info.info||'',tkid:info.tokenid||''};
  })()`, context, {timeout: 1500});
  if (typeof result.collect !== 'string' || !result.collect || result.collect.length > 300000 ||
      typeof result.eks !== 'string' || !result.eks || result.eks.length > 300000 ||
      typeof result.tkid !== 'string' || result.tkid.length > 10000) throw new Error();
  process.stdout.write(JSON.stringify(result));
} catch (_) { process.exitCode=1; }

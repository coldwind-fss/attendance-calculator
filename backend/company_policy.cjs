const { readFileSync } = require('node:fs');

const filename = process.env.HR_COMPANY_POLICY;
module.exports = filename ? JSON.parse(readFileSync(filename, 'utf8')) : {};

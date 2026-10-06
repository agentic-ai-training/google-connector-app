import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Status } from './status.jsx';

describe('reviewed offline React fixture', () => {
  it('renders the requested content', () => {
    expect(renderToStaticMarkup(<Status message="Verified fixture" />)).toContain('<h1>Verified fixture</h1>');
  });
  it('escapes untrusted HTML as text', () => {
    const html = renderToStaticMarkup(<Status message="<script>alert(1)</script>" />);
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });
});

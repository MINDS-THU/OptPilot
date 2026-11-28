import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const siteUrl = process.env.SITE_URL ?? 'https://minds-thu.github.io';
const baseUrl = process.env.SITE_BASE_URL ?? '/OptPilot/';

const config: Config = {
  title: 'OptPilot',
  tagline:
    'Compare candidate solutions on shared cases and use the results to guide the next iteration.',
  favicon: 'img/optpilot-mark.svg',

  future: {
    v4: true,
  },

  url: siteUrl,
  baseUrl,

  organizationName: 'MINDS-THU',
  projectName: 'OptPilot',

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-CN'],
    localeConfigs: {
      en: {
        label: 'English',
      },
      'zh-CN': {
        label: '中文',
      },
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'content/docs',
          sidebarPath: false,
        },
        blog: {
          path: 'content/blog',
          onUntruncatedBlogPosts: 'ignore',
          showReadingTime: true,
          blogTitle: 'Engineering Notes',
          blogDescription:
            'Release notes, architecture decisions, research context, and field notes from the OptPilot project.',
          blogSidebarTitle: 'All engineering notes',
          blogSidebarCount: 0,
        },
        pages: {
          path: 'src/pages',
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/optpilot-social-card.svg',
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: false,
    },
    navbar: {
          title: 'OptPilot',
      logo: {
        alt: 'MINDS-THU',
        src: 'img/MINDS-THU.png',
      },
      items: [
        {
          to: '/docs/intro',
          position: 'left',
          label: 'Overview',
        },
        {to: '/docs/demo', label: 'Desktop', position: 'left'},
        {to: '/docs/use-cases', label: 'Research Packages', position: 'left'},
        {to: '/blog', label: 'Engineering Notes', position: 'left'},
        {
          href: 'https://minds-thu.github.io/OptPilot/reference/',
          label: 'Documentation',
          position: 'right',
        },
        {
          href: 'https://github.com/MINDS-THU/OptPilot',
          label: 'GitHub',
          position: 'right',
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Product',
          items: [
            {
              label: 'Overview',
              to: '/docs/intro',
            },
            {
              label: 'OptPilot Desktop',
              to: '/docs/demo',
            },
            {
              label: 'Research Packages',
              to: '/docs/use-cases',
            },
          ],
        },
        {
          title: 'Learn',
          items: [
            {
              label: 'Getting Started',
            href: 'https://minds-thu.github.io/OptPilot/reference/getting-started/',
            },
            {
              label: 'Executable Capabilities',
            href: 'https://minds-thu.github.io/OptPilot/reference/capabilities/',
            },
            {
              label: 'Runs and Evidence',
            href: 'https://minds-thu.github.io/OptPilot/reference/evidence/',
            },
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/MINDS-THU/OptPilot',
            },
            {
              label: 'Issues',
              href: 'https://github.com/MINDS-THU/OptPilot/issues',
            },
            {
              label: 'Apache License 2.0',
              href: 'https://github.com/MINDS-THU/OptPilot/blob/main/LICENSE',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} OptPilot contributors · MINDS-THU.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
    metadata: [
      {
        name: 'keywords',
        content:
          'OptPilot, versioned experiments, experiment evidence, simulation optimization, MINDS-THU',
      },
    ],
  } satisfies Preset.ThemeConfig,
};

export default config;

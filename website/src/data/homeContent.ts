import {translate} from '@docusaurus/Translate';

export type ProofPoint = {
  value: string;
  label: string;
};

export type CapabilityBlock = {
  number: string;
  title: string;
  description: string;
  detail: string;
};

export type WorkflowStep = {
  number: string;
  title: string;
  description: string;
};

export const projectLinks = {
  desktop: '/docs/demo',
  researchPackages: '/docs/use-cases',
  engineeringNotes: '/blog',
  documentation: 'https://minds-thu.github.io/OptPilot/reference/',
  gettingStarted: 'https://minds-thu.github.io/OptPilot/reference/getting-started/',
  concepts: 'https://minds-thu.github.io/OptPilot/reference/concepts/',
  capabilities: 'https://minds-thu.github.io/OptPilot/reference/capabilities/',
  evidence: 'https://minds-thu.github.io/OptPilot/reference/evidence/',
  code: 'https://github.com/MINDS-THU/OptPilot',
  issues: 'https://github.com/MINDS-THU/OptPilot/issues',
  license: 'https://github.com/MINDS-THU/OptPilot/blob/main/LICENSE',
};

export const proofPoints: ProofPoint[] = [
  {
    value: '0.2.0',
    label: translate({id: 'homepage.proof.version', message: 'Core version'}),
  },
  {
    value: '3.10+',
    label: translate({id: 'homepage.proof.python', message: 'Python'}),
  },
  {
    value: 'Apache-2.0',
    label: translate({id: 'homepage.proof.license', message: 'Open source'}),
  },
  {
    value: 'Local-first',
    label: translate({id: 'homepage.proof.realm', message: 'CLI & Desktop'}),
  },
];

export const capabilityBlocks: CapabilityBlock[] = [
  {
    number: '01',
    title: translate({id: 'homepage.capabilities.contracts.title', message: 'Versioned artifacts'}),
    description: translate({
      id: 'homepage.capabilities.contracts.description',
      message:
        'Tasks define grading contracts and metrics. Instances provide evaluation cases. Suites, Candidates and optional Generators compose into reviewed Studies.',
    }),
    detail: translate({
      id: 'homepage.capabilities.contracts.detail',
      message: 'Tasks · Instances · Suites · Candidates',
    }),
  },
  {
    number: '02',
    title: translate({id: 'homepage.capabilities.execution.title', message: 'Frozen execution'}),
    description: translate({
      id: 'homepage.capabilities.execution.description',
      message:
        'Study review resolves selections to immutable versions and records the metric, budget and execution settings before a Run starts.',
    }),
    detail: translate({
      id: 'homepage.capabilities.execution.detail',
      message: 'Review first, then launch or resume',
    }),
  },
  {
    number: '03',
    title: translate({id: 'homepage.capabilities.evidence.title', message: 'Inspectable results'}),
    description: translate({
      id: 'homepage.capabilities.evidence.description',
      message:
        'Runs retain actual inputs, scores, failures and evidence across Trials and Attempts in the project experiment ledger.',
    }),
    detail: translate({
      id: 'homepage.capabilities.evidence.detail',
      message: 'Inspect versions, Trials and Attempts',
    }),
  },
  {
    number: '04',
    title: translate({id: 'homepage.capabilities.studio.title', message: 'OptPilot Desktop'}),
    description: translate({
      id: 'homepage.capabilities.studio.description',
      message:
        'A graphical companion to the public Core, using the same versioned project manifest and experiment ledger.',
    }),
    detail: translate({
      id: 'homepage.capabilities.studio.detail',
      message: 'Private source · installers not yet published',
    }),
  },
];

export const workflowSteps: WorkflowStep[] = [
  {
    number: '01',
    title: translate({id: 'homepage.workflow.define.title', message: 'Define'}),
    description: translate({
      id: 'homepage.workflow.define.description',
      message: 'Author Tasks, Instances, Suites, Candidates and Studies in optpilot.experiment.yaml.',
    }),
  },
  {
    number: '02',
    title: translate({id: 'homepage.workflow.validate.title', message: 'Validate'}),
    description: translate({
      id: 'homepage.workflow.validate.description',
      message: 'Validate the manifest, referenced files, artifact contracts and authored Studies.',
    }),
  },
  {
    number: '03',
    title: translate({id: 'homepage.workflow.run.title', message: 'Run'}),
    description: translate({
      id: 'homepage.workflow.run.description',
      message: 'Review freezes exact artifact versions. Start the Run with the returned review ID.',
    }),
  },
  {
    number: '04',
    title: translate({id: 'homepage.workflow.decide.title', message: 'Decide'}),
    description: translate({
      id: 'homepage.workflow.decide.description',
      message: 'Inspect Run, Trial and Attempt results from the shared experiment ledger.',
    }),
  },
];

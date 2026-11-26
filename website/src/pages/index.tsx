import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';
import {useBaseUrlUtils} from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import {
  capabilityBlocks,
  projectLinks,
  proofPoints,
  workflowSteps,
} from '@site/src/data/homeContent';
import {featuredUseCasePreviews} from '@site/src/content/listingData';
import type {CaseStudyTone} from '@site/src/content/listingData';

import styles from './index.module.css';

const toneClassMap: Record<CaseStudyTone, string> = {
  amber: styles.toneAmber,
  slate: styles.toneSlate,
  teal: styles.toneTeal,
};

function Arrow(): ReactNode {
  return <span aria-hidden="true">→</span>;
}

function HomepageHeader(): ReactNode {
  return (
    <header className={styles.hero}>
      <div className={`${styles.container} ${styles.heroLayout}`}>
        <div className={styles.heroCopy}>
          <p className={styles.kicker}>
            <Translate id="homepage.hero.eyebrow">Open-source experiment infrastructure</Translate>
            <span aria-hidden="true">/</span>
            <span>Python Core</span>
          </p>
          <Heading as="h1" className={styles.heroTitle}>
            <Translate id="homepage.hero.title">
              Experiments with frozen inputs and retained results.
            </Translate>
          </Heading>
          <p className={styles.heroLead}>
            <Translate id="homepage.hero.lead">
              Define tasks, evaluation cases, candidates and optional generators in a versioned project. Review freezes the selected versions before execution.
            </Translate>
          </p>
          <p className={styles.heroDetail}>
            <Translate id="homepage.hero.subtitle">
              The public Python Core and CLI run locally. OptPilot Desktop is a separate client with private source, and installer downloads are not published yet.
            </Translate>
          </p>
          <div className={styles.heroActions}>
            <Link className="button button--primary" href={projectLinks.gettingStarted}>
              <Translate id="homepage.hero.getStarted">Get started</Translate>
              <Arrow />
            </Link>
            <Link className="button button--secondary" to={projectLinks.desktop}>
              <Translate id="homepage.hero.exploreStudio">About Desktop</Translate>
            </Link>
            <Link className={styles.inlineLink} href={projectLinks.code}>
              GitHub <Arrow />
            </Link>
          </div>
          <ul
            className={styles.projectFacts}
            aria-label={translate({id: 'homepage.proof.ariaLabel', message: 'Project facts'})}>
            {proofPoints.map((point) => (
              <li key={point.label}>
                <strong>{point.value}</strong>
                <span>{point.label}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className={styles.manifestStage}>
          <div className={styles.manifestHeader}>
            <span><Translate id="homepage.manifest.label">Project manifest</Translate></span>
            <code>optpilot.experiment.yaml</code>
          </div>
          <div className={styles.manifestRows}>
            <div><span>01</span><code>tasks</code><strong><Translate id="homepage.manifest.tasks">grading contract + metrics</Translate></strong></div>
            <div><span>02</span><code>instances</code><strong><Translate id="homepage.manifest.instances">concrete evaluation cases</Translate></strong></div>
            <div><span>03</span><code>suites</code><strong><Translate id="homepage.manifest.suites">selected instance sets</Translate></strong></div>
            <div><span>04</span><code>candidates</code><strong><Translate id="homepage.manifest.candidates">parameter or program submissions</Translate></strong></div>
            <div><span>05</span><code>studies</code><strong><Translate id="homepage.manifest.studies">metric, budget and run conditions</Translate></strong></div>
          </div>
          <div className={styles.manifestFooter}>
            <span><Translate id="homepage.manifest.reviewed">Reviewed immutable versions</Translate></span>
            <span aria-hidden="true">→</span>
            <span><Translate id="homepage.manifest.run">Run · Trials · Attempts · evidence</Translate></span>
          </div>
        </div>
      </div>
    </header>
  );
}

function ArchitectureSection(): ReactNode {
  return (
    <section className={styles.section}>
      <div className={styles.container}>
        <div className={styles.sectionHeader}>
          <p className={styles.kicker}><Translate id="homepage.boundary.eyebrow">Reviewed versions are the execution boundary</Translate></p>
          <Heading as="h2">
            <Translate id="homepage.boundary.title">Review freezes the project before execution.</Translate>
          </Heading>
          <p>
            <Translate id="homepage.boundary.description">
              OptPilot validates experiment contracts, freezes selected artifact versions and records each run, trial, attempt and result.
            </Translate>
          </p>
        </div>

        <div
          className={styles.architecture}
          aria-label={translate({
            id: 'homepage.boundary.diagram.ariaLabel',
            message: 'Versioned experiment project reviewed and executed as a retained run',
          })}>
          <article>
            <span>01</span>
            <Heading as="h3"><Translate id="homepage.boundary.project.title">Project</Translate></Heading>
            <p><Translate id="homepage.boundary.project.text">Versioned Tasks, Instances, Suites, Candidates and optional Generators.</Translate></p>
          </article>
          <div className={styles.architectureArrow}><Translate id="homepage.boundary.reviewArrow">review →</Translate></div>
          <article className={styles.realm}>
            <span>02</span>
            <Heading as="h3"><Translate id="homepage.boundary.study.title">Reviewed Study</Translate></Heading>
            <p><Translate id="homepage.boundary.study.text">Freezes exact versions, metric, repetitions, budget and execution settings.</Translate></p>
          </article>
          <div className={styles.architectureArrow}><Translate id="homepage.boundary.executeArrow">execute →</Translate></div>
          <article>
            <span>03</span>
            <Heading as="h3"><Translate id="homepage.boundary.run.title">Run ledger</Translate></Heading>
            <p><Translate id="homepage.boundary.run.text">Records Trials, Attempts, scores, failures and evidence.</Translate></p>
          </article>
        </div>
        <div className={styles.evidenceLine}>
          <strong><Translate id="homepage.boundary.evidence.title">Shared project record</Translate></strong>
          <span><Translate id="homepage.boundary.evidence.text">Python CLI and Desktop inspect the same versions and experiment ledger.</Translate></span>
        </div>
      </div>
    </section>
  );
}

function CapabilitiesSection(): ReactNode {
  return (
    <section className={`${styles.section} ${styles.subtleSection}`} id="platform">
      <div className={`${styles.container} ${styles.capabilitiesLayout}`}>
        <div className={styles.stickyIntro}>
          <p className={styles.kicker}><Translate id="homepage.capabilities.eyebrow">A versioned project model</Translate></p>
          <Heading as="h2"><Translate id="homepage.capabilities.title">Define the experiment, then inspect what ran.</Translate></Heading>
          <p>
            <Translate id="homepage.capabilities.description">
              Tasks define grading contracts and metrics. Suites choose Instances. Candidates and optional Generators feed reviewed Studies.
            </Translate>
          </p>
          <Link className={styles.inlineLink} href={projectLinks.capabilities}>
            <Translate id="homepage.capabilities.boundary.link">Read the capability matrix</Translate> <Arrow />
          </Link>
        </div>

        <div className={styles.capabilityList}>
          {capabilityBlocks.map((block) => (
            <article key={block.number}>
              <span className={styles.itemNumber}>{block.number}</span>
              <div>
                <Heading as="h3">{block.title}</Heading>
                <p>{block.description}</p>
                <small>{block.detail}</small>
              </div>
            </article>
          ))}
          <aside className={styles.scopeNote}>
            <strong><Translate id="homepage.capabilities.boundary.title">Current executable boundary</Translate></strong>
            <p>
              <Translate id="homepage.capabilities.boundary.text">
                Native parameter and callable-program Candidates use trusted local Python grading. Harbor Candidates run through local Docker-backed jobs. Manual Tasks and registered POSIX SSH workers are also supported. Validation rejects unsupported artifact and runner combinations.
              </Translate>
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}

function WorkflowSection(): ReactNode {
  return (
    <section className={styles.section}>
      <div className={`${styles.container} ${styles.workflowLayout}`}>
        <div>
          <p className={styles.kicker}><Translate id="homepage.workflow.eyebrow">From manifest to retained result</Translate></p>
          <Heading as="h2"><Translate id="homepage.workflow.title">Review the exact inputs before a Run starts.</Translate></Heading>
          <ol className={styles.workflowList}>
            {workflowSteps.map((step) => (
              <li key={step.number}>
                <span>{step.number}</span>
                <div>
                  <Heading as="h3">{step.title}</Heading>
                  <p>{step.description}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>

        <div className={styles.quickStart}>
          <div className={styles.quickStartHeader}>
            <strong><Translate id="homepage.terminal.core">Core CLI</Translate></strong>
            <span>Python 3.10+</span>
          </div>
          <pre><code>uv sync{`\n\n`}optpilot validate catalog/optpilot_tutorial/optpilot.experiment.yaml{`\n\n`}optpilot experiment review --project catalog/optpilot_tutorial --study toy_factory-search{`\n`}# then use the returned review id{`\n`}optpilot experiment run --project catalog/optpilot_tutorial --review REVIEW_ID</code></pre>
          <p>
            <Translate id="homepage.terminal.note">
              The public repository contains the Python Core, CLI, examples and technical documentation. Desktop source is maintained privately.
            </Translate>
          </p>
        </div>
      </div>
    </section>
  );
}

function DesktopSection(): ReactNode {
  return (
    <section className={`${styles.section} ${styles.studioSection}`} id="desktop">
      <div className={styles.container}>
        <div className={styles.studioHeader}>
          <div>
            <p className={styles.kicker}><Translate id="homepage.studio.eyebrow">OptPilot Desktop</Translate></p>
            <Heading as="h2"><Translate id="homepage.studio.title">OptPilot Desktop</Translate></Heading>
          </div>
          <p>
            <Translate id="homepage.studio.description">
              Desktop is a graphical research workspace for the public Core's versioned project model. Conversations and writable directories sit beside a relationship map of saved artifact versions, evaluations and trial evidence.
            </Translate>
          </p>
        </div>

        <div className={styles.desktopDetails}>
          <aside className={styles.desktopAvailability}>
            <strong><Translate id="homepage.desktop.status.title">Desktop source is private</Translate></strong>
            <p><Translate id="homepage.desktop.status.text">After macOS, Windows and Linux builds pass release acceptance, installers will be published as public GitHub Release assets. Downloads are not available yet.</Translate></p>
          </aside>
          <div>
            <dl className={styles.studioFeatures}>
              <div>
                <dt><Translate id="homepage.studio.catalog.title">Versioned project artifacts</Translate></dt>
                <dd><Translate id="homepage.studio.catalog.text">Inspect Environments, Tasks, Instances, Suites, Candidates and Generators in the project relationship map.</Translate></dd>
              </div>
              <div>
                <dt><Translate id="homepage.studio.runSetups.title">Studies, Runs and Trials</Translate></dt>
                <dd><Translate id="homepage.studio.runSetups.text">Review frozen inputs, start evaluations explicitly, and inspect per-instance scores, failures and evidence.</Translate></dd>
              </div>
              <div>
                <dt><Translate id="homepage.studio.workspaces.title">Versioned editing</Translate></dt>
                <dd><Translate id="homepage.studio.workspaces.text">Edit a draft, review file changes and save a new version without changing earlier results.</Translate></dd>
              </div>
              <div>
                <dt><Translate id="homepage.studio.assistant.title">Conversations and assistants</Translate></dt>
                <dd><Translate id="homepage.studio.assistant.text">Work with connected coding assistants and send recorded evaluation evidence back into a conversation.</Translate></dd>
              </div>
            </dl>
            <div className={styles.sectionActions}>
              <Link className="button button--primary" href={projectLinks.gettingStarted}>
                <Translate id="homepage.studio.cta">Use the public Core</Translate> <Arrow />
              </Link>
              <Link className={styles.inlineLink} href={projectLinks.documentation}>
                <Translate id="homepage.studio.docs">Read the documentation</Translate> <Arrow />
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function ResearchPackagesSection(): ReactNode {
  const {withBaseUrl} = useBaseUrlUtils();

  return (
    <section className={`${styles.section} ${styles.researchSection}`} id="research">
      <div className={styles.container}>
        <div className={styles.researchHeader}>
          <div className={styles.sectionHeader}>
            <p className={styles.kicker}><Translate id="homepage.research.eyebrow">Research packages</Translate></p>
            <Heading as="h2"><Translate id="homepage.research.title">Research that runs on the same public contract.</Translate></Heading>
          </div>
          <div>
            <p>
              <Translate id="homepage.research.description">
                Paper companion projects exercise simulation generation, policy design and operations-research solving through the current experiment model.
              </Translate>
            </p>
            <Link className={styles.inlineLink} to={projectLinks.researchPackages}>
              <Translate id="homepage.research.viewAll">Explore all packages</Translate> <Arrow />
            </Link>
          </div>
        </div>

        <div className={styles.researchGallery}>
          {featuredUseCasePreviews.map((caseStudy, index) => (
            <Link key={caseStudy.title} to={caseStudy.href} className={styles.researchCard}>
              <div className={styles.researchVisual}>
                {caseStudy.previewImage ? (
                  <img
                    src={withBaseUrl(caseStudy.previewImage)}
                    alt={caseStudy.imageTitle}
                  />
                ) : (
                  <div className={`${styles.researchFallback} ${toneClassMap[caseStudy.tone]}`}>
                    <span>OPTPILOT / RESEARCH</span>
                    <strong>{caseStudy.imageTitle}</strong>
                    <p>{caseStudy.imageCaption}</p>
                  </div>
                )}
                <span className={styles.researchIndex}>0{index + 1}</span>
              </div>
              <div className={styles.researchCardBody}>
                <div className={styles.researchMeta}>
                  <span>{caseStudy.metric}</span>
                  <span className={styles.researchArrow} aria-hidden="true">↗</span>
                </div>
                <Heading as="h3">{caseStudy.title}</Heading>
                <p>{caseStudy.abstract}</p>
                <div className={styles.researchTags} aria-hidden="true">
                  {caseStudy.tags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
              </div>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}

function FinalCallToAction(): ReactNode {
  return (
    <section className={styles.finalSection}>
      <div className={`${styles.container} ${styles.finalLayout}`}>
        <div>
          <p className={styles.kicker}><Translate id="homepage.final.eyebrow">Make the next run inspectable</Translate></p>
          <Heading as="h2"><Translate id="homepage.final.title">Freeze the inputs. Keep the evidence.</Translate></Heading>
        </div>
        <div>
          <p>
            <Translate id="homepage.final.description">
              Start with the tutorial project, review a Study, and inspect its Run evidence with the CLI.
            </Translate>
          </p>
          <div className={styles.sectionActions}>
            <Link className="button button--primary" href={projectLinks.gettingStarted}>
              <Translate id="homepage.final.getStarted">Run the tutorial</Translate> <Arrow />
            </Link>
            <Link className={styles.inlineLink} href={projectLinks.code}>
              <Translate id="homepage.final.github">View on GitHub</Translate> <Arrow />
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title={translate({
        id: 'homepage.meta.title',
        message: 'Inspectable infrastructure for iterative optimization',
      })}
      description={translate({
        id: 'homepage.meta.description',
        message: 'OptPilot provides versioned experiment projects, frozen Study reviews and inspectable Run evidence.',
      })}>
      <HomepageHeader />
      <main>
        <ArchitectureSection />
        <CapabilitiesSection />
        <WorkflowSection />
        <DesktopSection />
        <ResearchPackagesSection />
        <FinalCallToAction />
      </main>
    </Layout>
  );
}

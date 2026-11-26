import React, {type ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Translate from '@docusaurus/Translate';
import styles from './styles.module.css';

type Item = {title: ReactNode; text: ReactNode};

const coreConcepts: Item[] = [
  {title: <Translate id="overview.task.title">Task and Instance</Translate>, text: <Translate id="overview.task.text">A Task defines the grading contract and metrics. An Instance supplies one concrete evaluation case and its inputs.</Translate>},
  {title: <Translate id="overview.suite.title">Suite and Candidate</Translate>, text: <Translate id="overview.suite.text">A Suite selects Instances. A Candidate is an actual parameter set, callable program, or supported Harbor agent configuration.</Translate>},
  {title: <Translate id="overview.study.title">Generator and Study</Translate>, text: <Translate id="overview.study.text">An optional Generator proposes Candidates. A Study selects exact artifacts and sets the metric, budget, repetitions and execution conditions.</Translate>},
  {title: <Translate id="overview.run.title">Run, Trial and Attempt</Translate>, text: <Translate id="overview.run.text">A Run executes a reviewed Study revision. Each Candidate–Instance–repetition is a Trial, and a retry creates another Attempt.</Translate>},
];

function RunLoop(): ReactNode {
  return (
    <figure className={styles.runLoop} aria-label="OptPilot reviewed experiment workflow">
      <div className={styles.runLoopRow}>
        <div><span>01</span><strong><Translate id="overview.diagram.project">Project</Translate></strong><p><Translate id="overview.diagram.projectText">Define Tasks, cases, Suites and Candidates</Translate></p></div>
        <span aria-hidden="true">→</span>
        <div className={styles.realm}><span>02</span><strong><Translate id="overview.diagram.study">Reviewed Study</Translate></strong><p><Translate id="overview.diagram.studyText">Freeze versions and run conditions</Translate></p></div>
        <span aria-hidden="true">→</span>
        <div><span>03</span><strong><Translate id="overview.diagram.run">Run ledger</Translate></strong><p><Translate id="overview.diagram.runText">Record Trials, Attempts and results</Translate></p></div>
      </div>
      <figcaption><strong><Translate id="overview.diagram.ledger">Shared project experiment ledger</Translate></strong><span><Translate id="overview.diagram.evidence">Actual inputs · scores · failures · evidence</Translate></span></figcaption>
    </figure>
  );
}

export default function OverviewPage(): ReactNode {
  return (
    <div className={styles.page}>
      <header className={styles.intro}>
        <p className={styles.releaseLine}><Translate id="overview.release">OptPilot Core 0.2.0 · Python 3.10+ · Apache-2.0</Translate></p>
        <h1><Translate id="overview.title">OptPilot at a glance</Translate></h1>
        <p className={styles.lede}><Translate id="overview.lead">OptPilot is an open-source Python package and CLI for versioned, measured experiments. A project manifest defines the evaluation contract, concrete cases, candidate submissions and reviewed execution plans.</Translate></p>
        <p><Translate id="overview.desktopIntro">OptPilot Desktop is a separately distributed client with private source. Both entry points use the same project manifest and</Translate> <code>.optpilot/experiments</code> <Translate id="overview.desktopOutro">ledger. Desktop installers are not publicly available yet.</Translate></p>
      </header>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.why.eyebrow">Why it exists</Translate></p><h2><Translate id="overview.why.title">Keep each experiment tied to its actual inputs.</Translate></h2></div>
        <div className={styles.proseColumns}>
          <p><Translate id="overview.why.p1">Evaluation work spans authored contracts, concrete cases, executable submissions, run conditions and external services. When those pieces change independently, it becomes difficult to know which inputs produced a score.</Translate></p>
          <p><Translate id="overview.why.p2">OptPilot saves artifact versions, validates a Study selection, and freezes its exact inputs before execution. Each Run preserves the versions it used and the evidence needed to inspect its outcome.</Translate></p>
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.model.eyebrow">Project model</Translate></p><h2><Translate id="overview.model.title">Artifacts compose into a reviewed Study.</Translate></h2></div>
        <div className={styles.itemList}>{coreConcepts.map((item, index) => <article key={index}><span>0{index + 1}</span><h3>{item.title}</h3><p>{item.text}</p></article>)}</div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.lifecycle.eyebrow">Run lifecycle</Translate></p><h2><Translate id="overview.lifecycle.title">Review freezes the plan. A Run records what happened.</Translate></h2></div>
        <p className={styles.sectionLead}><Translate id="overview.lifecycle.text">A Study may compare fixed Candidates, use a feedback-driven Generator, produce a single generated solution, or run supported Harbor agent configurations against a frozen task package.</Translate></p>
        <RunLoop />
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.contract.eyebrow">Candidate contracts</Translate></p><h2><Translate id="overview.contract.title">Review checks artifacts before execution.</Translate></h2></div>
        <p className={styles.sectionLead}><Translate id="overview.contract.text">Native Tasks accept parameter or callable-program Candidates. Harbor Tasks use supported agent configurations with a Harbor task package. Review validates the selected versions and supported runner before launch.</Translate></p>
        <div className={styles.contractList}>
          <article><strong><Translate id="overview.contract.task">Task declares</Translate></strong><span><Translate id="overview.contract.taskText">Grader, Candidate contract, metrics and optional feedback metrics.</Translate></span></article>
          <article><strong><Translate id="overview.contract.candidate">Candidate declares</Translate></strong><span><Translate id="overview.contract.candidateText">Parameter values, a callable entrypoint and files, or supported Harbor agent configuration.</Translate></span></article>
          <article><strong><Translate id="overview.contract.review">Study review records</Translate></strong><span><Translate id="overview.contract.reviewText">Immutable artifact versions, metric, budget, repetitions and selected runner.</Translate></span></article>
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.execution.eyebrow">Execution and trust</Translate></p><h2><Translate id="overview.execution.title">Choose a runner that matches the work.</Translate></h2></div>
        <div className={styles.boundaryColumns}>
          <div><h3><Translate id="overview.execution.paths">Available paths</Translate></h3><ul>
            <li><Translate id="overview.execution.native">Native parameter and callable-program Candidates</Translate></li>
            <li><Translate id="overview.execution.python">Trusted local Python grading in separate worker processes</Translate></li>
            <li><Translate id="overview.execution.generator">Optional feedback-driven Candidate Generators</Translate></li>
            <li><Translate id="overview.execution.harbor">Harbor agent configurations through local Docker-backed jobs</Translate></li>
            <li><Translate id="overview.execution.manual">Manual Tasks, registered resource plans and POSIX SSH workers</Translate></li>
          </ul></div>
          <div><h3><Translate id="overview.execution.boundary">Execution boundary</Translate></h3><p><Translate id="overview.execution.boundaryText">Local Python runs trusted project code and is not an adversarial sandbox. Harbor uses its own task isolation. Unsupported artifact and runner combinations fail validation before execution.</Translate></p><Link href="https://minds-thu.github.io/OptPilot/reference/capabilities/"><Translate id="overview.execution.link">Read executable capabilities →</Translate></Link></div>
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHeading}><p><Translate id="overview.desktop.eyebrow">Desktop client</Translate></p><h2><Translate id="overview.desktop.title">A graphical entry point to the same project ledger.</Translate></h2></div>
        <div className={styles.itemList}>
          <article><span>01</span><h3><Translate id="overview.desktop.sharedTitle">Shared experiment model</Translate></h3><p><Translate id="overview.desktop.sharedText">Desktop and the Python CLI inspect the same manifest, frozen artifact versions and Run results.</Translate></p></article>
          <article><span>02</span><h3><Translate id="overview.desktop.privateTitle">Private source</Translate></h3><p><Translate id="overview.desktop.privateText">Desktop source is maintained in a separate private repository. The public repository contains Core, examples and documentation.</Translate></p></article>
          <article><span>03</span><h3><Translate id="overview.desktop.statusTitle">Distribution status</Translate></h3><p><Translate id="overview.desktop.statusText">Installers are not yet published to public GitHub Releases.</Translate></p></article>
        </div>
      </section>

      <nav className={styles.nextSteps} aria-label="Explore OptPilot">
        <p><Translate id="overview.next.eyebrow">Explore next</Translate></p>
        <Link to="/docs/demo"><strong><Translate id="overview.next.desktop">OptPilot Desktop</Translate></strong><span><Translate id="overview.next.desktopText">Read the client overview and distribution status.</Translate></span><b>→</b></Link>
        <Link to="/docs/use-cases"><strong><Translate id="overview.next.packages">Research Packages</Translate></strong><span><Translate id="overview.next.packagesText">Explore DEVS-Gen, trace-guided heuristic design, and COOPA.</Translate></span><b>→</b></Link>
        <Link href="https://minds-thu.github.io/OptPilot/reference/getting-started/"><strong><Translate id="overview.next.start">Getting Started</Translate></strong><span><Translate id="overview.next.startText">Validate a project, review a Study and inspect its Run.</Translate></span><b>→</b></Link>
      </nav>
    </div>
  );
}

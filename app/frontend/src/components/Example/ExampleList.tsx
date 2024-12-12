// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import { Example } from "./Example";

import styles from "./Example.module.css";

export type ExampleModel = {
    text: string;
    value: string;
};

const EXAMPLES: ExampleModel[] = [
    { text: "What is the employee dress code?", value: "What is the employee dress code?" },
    { text: "What are the different rules associated with Tier IV Chapter 504?", value: "What are the different rules associated with Tier IV Chapter 504?" },
    { text: "List and explain some recent board resolutions and there impact on TRS.", value: "List and explain some recent board resolutions and there impact on TRS." }
];

interface Props {
    onExampleClicked: (value: string) => void;
}

export const ExampleList = ({ onExampleClicked }: Props) => {
    return (
        <ul className={styles.examplesNavList}>
            {EXAMPLES.map((x, i) => (
                <li key={i}>
                    <Example text={x.text} value={x.value} onClick={onExampleClicked} />
                </li>
            ))}
        </ul>
    );
};

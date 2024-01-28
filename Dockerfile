# Use the official OpenJDK 11 image as the base image
FROM openjdk:11-jre-slim

ENTRYPOINT ["top", "-b"]
